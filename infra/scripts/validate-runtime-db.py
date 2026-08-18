#!/usr/bin/env python3
"""Validate the production runtime role without leaving probe data behind."""

from __future__ import annotations

import os
from uuid import uuid4

from sqlalchemy import text

from ai_research_radar.db import create_db_engine, validate_production_schema


RUNTIME_ROLE = "radar_runtime"
RUNTIME_TABLES = frozenset(
    {
        "issuer_master",
        "sources",
        "source_cursors",
        "ingestion_runs",
        "items",
        "item_versions",
        "events",
        "event_revisions",
        "event_items",
        "evidence",
        "deliveries",
        "delivery_event_revisions",
        "webhook_events",
        "source_health",
        "usage_ledger",
    }
)
RUNTIME_TABLE_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
RUNTIME_SETTINGS = frozenset(
    {
        "search_path=pg_catalog, public, pg_temp",
        "row_security=on",
        "statement_timeout=20min",
        "idle_in_transaction_session_timeout=2min",
    }
)


def _require_exact(
    label: str,
    actual: set[tuple[object, ...]],
    expected: set[tuple[object, ...]],
) -> None:
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            f"Unexpected {label}; missing={missing!r}, unexpected={unexpected!r}"
        )


def _validate_role_boundary(connection) -> None:  # type: ignore[no-untyped-def]
    role = connection.execute(
        text(
            "select rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolinherit, rolbypassrls, rolconnlimit, rolconfig "
            "from pg_roles where rolname = current_user"
        )
    ).mappings().one()
    expected_attributes = {
        "rolcanlogin": True,
        "rolsuper": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolinherit": False,
        "rolbypassrls": False,
        "rolconnlimit": 8,
    }
    for attribute, expected in expected_attributes.items():
        if role[attribute] != expected:
            raise RuntimeError(
                f"Unexpected runtime role attribute {attribute}: {role[attribute]!r}"
            )
    settings = frozenset(role["rolconfig"] or ())
    if settings != RUNTIME_SETTINGS:
        raise RuntimeError(
            f"Unexpected runtime role settings: {sorted(settings)!r}"
        )

    memberships = {
        (row.parent_role,)
        for row in connection.execute(
            text(
                "select parent.rolname as parent_role "
                "from pg_auth_members membership "
                "join pg_roles child on child.oid = membership.member "
                "join pg_roles parent on parent.oid = membership.roleid "
                "where child.rolname = current_user"
            )
        )
    }
    _require_exact("role memberships", memberships, set())

    database_grants = {
        (row.database_name, row.privilege_type, row.is_grantable)
        for row in connection.execute(
            text(
                "select database.datname as database_name, acl.privilege_type, "
                "acl.is_grantable "
                "from pg_database database "
                "cross join lateral aclexplode(database.datacl) acl "
                "join pg_roles grantee on grantee.oid = acl.grantee "
                "where grantee.rolname = current_user"
            )
        )
    }
    _require_exact(
        "direct database grants",
        database_grants,
        {("postgres", "CONNECT", False)},
    )

    schema_grants = {
        (row.schema_name, row.privilege_type, row.is_grantable)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, acl.privilege_type, "
                "acl.is_grantable "
                "from pg_namespace namespace "
                "cross join lateral aclexplode(namespace.nspacl) acl "
                "join pg_roles grantee on grantee.oid = acl.grantee "
                "where grantee.rolname = current_user"
            )
        )
    }
    _require_exact(
        "direct schema grants",
        schema_grants,
        {("public", "USAGE", False)},
    )

    table_grants = {
        (row.schema_name, row.table_name, row.privilege_type, row.is_grantable)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, relation.relname as table_name, "
                "acl.privilege_type, acl.is_grantable "
                "from pg_class relation "
                "join pg_namespace namespace on namespace.oid = relation.relnamespace "
                "cross join lateral aclexplode(relation.relacl) acl "
                "join pg_roles grantee on grantee.oid = acl.grantee "
                "where grantee.rolname = current_user "
                "and relation.relkind in ('r', 'p', 'v', 'm', 'f', 'S')"
            )
        )
    }
    expected_table_grants = {
        ("public", table_name, privilege, False)
        for table_name in RUNTIME_TABLES
        for privilege in RUNTIME_TABLE_PRIVILEGES
    }
    _require_exact("direct table grants", table_grants, expected_table_grants)

    column_grants = {
        (
            row.schema_name,
            row.table_name,
            row.column_name,
            row.privilege_type,
            row.is_grantable,
        )
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, relation.relname as table_name, "
                "attribute.attname as column_name, acl.privilege_type, acl.is_grantable "
                "from pg_attribute attribute "
                "join pg_class relation on relation.oid = attribute.attrelid "
                "join pg_namespace namespace on namespace.oid = relation.relnamespace "
                "cross join lateral aclexplode(attribute.attacl) acl "
                "join pg_roles grantee on grantee.oid = acl.grantee "
                "where grantee.rolname = current_user "
                "and attribute.attnum > 0 and not attribute.attisdropped"
            )
        )
    }
    _require_exact("direct column grants", column_grants, set())

    routine_grants = {
        (row.schema_name, row.routine_name, row.privilege_type, row.is_grantable)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, routine.proname as routine_name, "
                "acl.privilege_type, acl.is_grantable "
                "from pg_proc routine "
                "join pg_namespace namespace on namespace.oid = routine.pronamespace "
                "cross join lateral aclexplode(routine.proacl) acl "
                "join pg_roles grantee on grantee.oid = acl.grantee "
                "where grantee.rolname = current_user"
            )
        )
    }
    _require_exact("direct routine grants", routine_grants, set())

    default_grants = {
        (
            row.owner_name,
            row.schema_name or "<all>",
            row.object_type,
            row.privilege_type,
            row.is_grantable,
        )
        for row in connection.execute(
            text(
                "select owner.rolname as owner_name, namespace.nspname as schema_name, "
                "defaults.defaclobjtype as object_type, acl.privilege_type, "
                "acl.is_grantable "
                "from pg_default_acl defaults "
                "join pg_roles owner on owner.oid = defaults.defaclrole "
                "left join pg_namespace namespace on namespace.oid = defaults.defaclnamespace "
                "cross join lateral aclexplode(defaults.defaclacl) acl "
                "join pg_roles grantee on grantee.oid = acl.grantee "
                "where grantee.rolname = current_user"
            )
        )
    }
    _require_exact("default grants", default_grants, set())

    effective_schemas = {
        (row.schema_name, row.can_use, row.can_create)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, "
                "has_schema_privilege(current_user, namespace.oid, 'USAGE') as can_use, "
                "has_schema_privilege(current_user, namespace.oid, 'CREATE') as can_create "
                "from pg_namespace namespace "
                "where namespace.nspname !~ '^pg_' "
                "and namespace.nspname <> 'information_schema' "
                "and (has_schema_privilege(current_user, namespace.oid, 'USAGE') "
                "or has_schema_privilege(current_user, namespace.oid, 'CREATE'))"
            )
        )
    }
    _require_exact(
        "effective non-system schema privileges",
        effective_schemas,
        {("public", True, False)},
    )

    effective_table_grants = {
        (row.schema_name, row.table_name, row.privilege_type)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, relation.relname as table_name, "
                "privilege.privilege_type "
                "from pg_class relation "
                "join pg_namespace namespace on namespace.oid = relation.relnamespace "
                "cross join (values ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), "
                "('TRUNCATE'), ('REFERENCES'), ('TRIGGER'), ('MAINTAIN')) "
                "as privilege(privilege_type) "
                "where namespace.nspname !~ '^pg_' "
                "and namespace.nspname <> 'information_schema' "
                "and relation.relkind in ('r', 'p', 'v', 'm', 'f', 'S') "
                "and has_schema_privilege(current_user, namespace.oid, 'USAGE') "
                "and has_table_privilege("
                "current_user, relation.oid, privilege.privilege_type)"
            )
        )
    }
    expected_effective_table_grants = {
        ("public", table_name, privilege)
        for table_name in RUNTIME_TABLES
        for privilege in RUNTIME_TABLE_PRIVILEGES
    }
    _require_exact(
        "effective non-system table grants",
        effective_table_grants,
        expected_effective_table_grants,
    )

    effective_column_relations = {
        (row.schema_name, row.table_name)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, relation.relname as table_name "
                "from pg_class relation "
                "join pg_namespace namespace on namespace.oid = relation.relnamespace "
                "where namespace.nspname !~ '^pg_' "
                "and namespace.nspname <> 'information_schema' "
                "and relation.relkind in ('r', 'p', 'v', 'm', 'f') "
                "and has_schema_privilege(current_user, namespace.oid, 'USAGE') "
                "and has_any_column_privilege("
                "current_user, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES')"
            )
        )
    }
    _require_exact(
        "effective non-system column grants",
        effective_column_relations,
        {("public", table_name) for table_name in RUNTIME_TABLES},
    )

    executable_routines = {
        (row.schema_name, row.routine_name)
        for row in connection.execute(
            text(
                "select namespace.nspname as schema_name, "
                "routine.oid::regprocedure::text as routine_name "
                "from pg_proc routine "
                "join pg_namespace namespace on namespace.oid = routine.pronamespace "
                "where namespace.nspname !~ '^pg_' "
                "and namespace.nspname <> 'information_schema' "
                "and has_schema_privilege(current_user, namespace.oid, 'USAGE') "
                "and has_function_privilege(current_user, routine.oid, 'EXECUTE')"
            )
        )
    }
    _require_exact(
        "effective non-system routine privileges",
        executable_routines,
        set(),
    )

    ownership = {
        (row.object_type, row.object_name)
        for row in connection.execute(
            text(
                "select 'relation' as object_type, "
                "format('%I.%I', namespace.nspname, relation.relname) as object_name "
                "from pg_class relation "
                "join pg_namespace namespace on namespace.oid = relation.relnamespace "
                "where relation.relowner = (select oid from pg_roles where rolname=current_user) "
                "union all "
                "select 'routine', routine.oid::regprocedure::text "
                "from pg_proc routine "
                "where routine.proowner = (select oid from pg_roles where rolname=current_user) "
                "union all "
                "select 'schema', namespace.nspname "
                "from pg_namespace namespace "
                "where namespace.nspowner = (select oid from pg_roles where rolname=current_user) "
                "union all "
                "select 'database', database.datname "
                "from pg_database database "
                "where database.datdba = (select oid from pg_roles where rolname=current_user)"
            )
        )
    }
    _require_exact("owned database objects", ownership, set())

    database_capabilities = connection.execute(
        text(
            "select has_database_privilege(current_user, oid, 'CONNECT') as can_connect, "
            "has_database_privilege(current_user, oid, 'CREATE') as can_create "
            "from pg_database where datname = current_database()"
        )
    ).mappings().one()
    if not database_capabilities["can_connect"] or database_capabilities["can_create"]:
        raise RuntimeError("Unexpected effective database-level privileges")

    policies = {
        (
            row.schemaname,
            row.tablename,
            row.policyname,
            row.permissive,
            tuple(row.policy_roles),
            row.cmd,
            row.qual,
            row.with_check,
        )
        for row in connection.execute(
            text(
                "select schemaname, tablename, policyname, permissive, "
                "roles::text[] as policy_roles, cmd, qual, with_check "
                "from pg_policies "
                "where current_user::text = any(roles::text[])"
            )
        )
    }
    expected_policies = {
        (
            "public",
            table_name,
            "radar_runtime_all",
            "PERMISSIVE",
            (RUNTIME_ROLE,),
            "ALL",
            "true",
            "true",
        )
        for table_name in RUNTIME_TABLES
    }
    _require_exact("RLS policies", policies, expected_policies)

    rls_rows = connection.execute(
        text(
            "select relation.relname as table_name, relation.relrowsecurity as rls_enabled, "
            "row_security_active(relation.oid) as rls_active, owner.rolname as owner_name "
            "from pg_class relation "
            "join pg_namespace namespace on namespace.oid = relation.relnamespace "
            "join pg_roles owner on owner.oid = relation.relowner "
            "where namespace.nspname = 'public' "
            "and relation.relname::text = any(cast(:table_names as text[]))"
        ),
        {"table_names": sorted(RUNTIME_TABLES)},
    ).mappings().all()
    if {row["table_name"] for row in rls_rows} != RUNTIME_TABLES:
        raise RuntimeError("RLS audit did not return exactly the runtime tables")
    for row in rls_rows:
        if not row["rls_enabled"] or not row["rls_active"]:
            raise RuntimeError(f"RLS is not active for public.{row['table_name']}")
        if row["owner_name"] == RUNTIME_ROLE:
            raise RuntimeError(f"Runtime role unexpectedly owns public.{row['table_name']}")


def main() -> None:
    database_url = os.environ.get("RADAR_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("RADAR_DATABASE_URL is required")

    engine = create_db_engine(database_url)
    probe_key = f"__radar_runtime_probe_{uuid4().hex}"
    try:
        validate_production_schema(engine)
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                current_user = connection.scalar(text("select current_user"))
                if current_user != RUNTIME_ROLE:
                    raise RuntimeError(
                        f"Expected {RUNTIME_ROLE}, connected as {current_user!r}"
                    )
                _validate_role_boundary(connection)

                connection.execute(
                    text(
                        "insert into public.usage_ledger "
                        "(usage_date, usage_key, used, hard_limit) "
                        "values (current_date, :usage_key, 0, 1)"
                    ),
                    {"usage_key": probe_key},
                )
                connection.execute(
                    text(
                        "update public.usage_ledger set used = 1 "
                        "where usage_date = current_date and usage_key = :usage_key"
                    ),
                    {"usage_key": probe_key},
                )
                used = connection.scalar(
                    text(
                        "select used from public.usage_ledger "
                        "where usage_date = current_date and usage_key = :usage_key"
                    ),
                    {"usage_key": probe_key},
                )
                if used != 1:
                    raise RuntimeError("Runtime role CRUD probe returned an invalid value")
                connection.execute(
                    text(
                        "delete from public.usage_ledger "
                        "where usage_date = current_date and usage_key = :usage_key"
                    ),
                    {"usage_key": probe_key},
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    print("radar_runtime privilege boundary and rollback CRUD probe passed")


if __name__ == "__main__":
    main()
