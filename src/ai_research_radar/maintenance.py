"""Read-only maintenance diagnostics; cleanup requires a separately approved plan."""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import re

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import (
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    SourceHealthModel,
    SourceModel,
    UsageLedgerModel,
)

CAPACITY_WARNING_BYTES = 350 * 1024 * 1024


@contextmanager
def readonly_session(factory):
    """Enforce read-only SQL and always roll back, including diagnostic failures."""
    with factory() as session:
        connection = session.connection()
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            connection.exec_driver_sql("PRAGMA query_only=ON")
        else:
            session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield session
        finally:
            # Reset the connection-local SQLite flag before returning to the pool.
            if sqlite:
                connection.exec_driver_sql("PRAGMA query_only=OFF")
            session.rollback()


def diagnose(session, settings, *, preview=False, raw_store=None, storage_skip_reason=None):
    cutoff = datetime.now(UTC) - timedelta(days=14)
    references = session.execute(
        select(ItemVersionModel.id, ItemVersionModel.raw_storage_path)
        .where(ItemVersionModel.raw_storage_path.is_not(None), ItemVersionModel.fetched_at < cutoff)
        .order_by(ItemVersionModel.id)
    ).all()
    paths = sorted({path for _, path in references if path})
    storage_paths = None
    storage_error = None
    if raw_store is not None:
        try:
            storage_paths = sorted(set(raw_store.list_older_than(cutoff.date())))
        except Exception as exc:
            # HTTP errors can contain credential-bearing URLs; report only safe types.
            storage_error = {"error_type": type(exc).__name__}
            storage_skip_reason = "storage_listing_failed"
    candidates = sorted(set(paths) | set(storage_paths or []))
    # Orphan means unreferenced by ANY version, including retained/newer versions.
    referenced_paths = (
        set(
            session.scalars(
                select(ItemVersionModel.raw_storage_path).where(
                    ItemVersionModel.raw_storage_path.is_not(None)
                )
            )
        )
        if storage_paths is not None
        else set()
    )
    orphans = sorted(set(storage_paths or []) - referenced_paths)
    retained_references = set(
        session.scalars(
            select(ItemVersionModel.raw_storage_path).where(
                ItemVersionModel.raw_storage_path.is_not(None),
                ItemVersionModel.fetched_at >= cutoff,
            )
        )
    )
    protected = sorted(set(candidates) & retained_references)
    eligible = sorted(set(candidates) - retained_references)
    failures = []
    for health, source in session.execute(
        select(SourceHealthModel, SourceModel)
        .outerjoin(SourceModel, SourceModel.id == SourceHealthModel.source_id)
        .where(SourceHealthModel.consecutive_failures >= 3)
        .order_by(SourceHealthModel.source_id)
    ):
        metadata = health.metadata_json or {}
        error_type = metadata.get("error_type")
        if not error_type:
            # Extract only the constrained class token from old safe boundary records.
            match = re.search(r"error_type=([A-Za-z]+)", health.last_error or "")
            error_type = match.group(1) if match else None
            if (health.last_error or "").startswith("collection budget exhausted: stage="):
                error_type = "CollectionBudgetExceeded"
        failures.append(
            {
                "source_id": health.source_id,
                "status": health.status,
                "consecutive_failures": health.consecutive_failures,
                "last_http_status": health.last_http_status,
                "last_attempt_at": health.last_attempt_at,
                "last_success_at": health.last_success_at,
                "last_latency_ms": health.last_latency_ms,
                "group": source.group if source else None,
                "enabled": source.enabled if source else None,
                "next_due_at": source.next_due_at if source else None,
                # Never print legacy last_error, request URLs, arbitrary metadata or credentials.
                "error_type": error_type
                if error_type
                in {
                    "CollectorHTTPError",
                    "CollectionBudgetExceeded",
                    "OperationalError",
                    "IntegrityError",
                    "RuntimeError",
                    "TimeoutError",
                }
                else None,
            }
        )
    engine = session.get_bind()
    relations = None
    relation_error = None
    if engine.dialect.name == "sqlite":
        path = settings.database_url.removeprefix("sqlite:///")
        database_bytes = Path(path).stat().st_size if path and Path(path).exists() else 0
    else:
        database_bytes = int(
            session.scalar(text("SELECT pg_database_size(current_database())")) or 0
        )
        relations, relation_error = relation_sizes(session)
    reasons = []
    if database_bytes >= CAPACITY_WARNING_BYTES:
        reasons.append("database_capacity_warning")
    if failures:
        reasons.append("persistent_source_failures")
    if settings.app_env == "production" and references:
        reasons.append("expired_raw_references_pending")
    if storage_error or storage_skip_reason == "missing_storage_credentials":
        reasons.append("storage_preview_unavailable")
    payload = {
        "read_only": True,
        "preview": preview,
        "items": session.scalar(select(func.count()).select_from(ItemModel)),
        "versions": session.scalar(select(func.count()).select_from(ItemVersionModel)),
        "events": session.scalar(select(func.count()).select_from(RadarEventModel)),
        "database_bytes": database_bytes,
        "capacity_warning_bytes": CAPACITY_WARNING_BYTES,
        "capacity_warning_350mb": database_bytes >= CAPACITY_WARNING_BYTES,
        "capacity_hard_limit_bytes": None,
        "database_relations": relations,
        "relation_diagnostics_error": relation_error,
        "sources_failing": [f["source_id"] for f in failures],
        "source_failure_details": failures,
        "failure_reasons": reasons,
        "cleanup_skip_reason": "read_only_diagnostics",
        "storage_listing_skip_reason": storage_skip_reason,
        "expired_raw_objects_removed": 0,
        # Compatibility field counts DB references, NOT verified bucket objects.
        "expired_raw_objects_pending": len(references),
        "expired_raw_reference_count": len(references),
        "expired_raw_unique_path_count": len(paths),
        "storage_listing_performed": storage_paths is not None,
        "storage_listing_error": storage_error,
        "storage_expired_object_count": len(storage_paths) if storage_paths is not None else None,
        "storage_orphan_count": len(orphans) if storage_paths is not None else None,
        "cleanup_candidate_unique_path_count": len(eligible),
        "protected_recent_reference_path_count": len(protected),
        "raw_cutoff_exclusive": cutoff,
        "storage_date_cutoff_exclusive": cutoff.date(),
        "expired_usage_ledger_rows": session.scalar(
            select(func.count())
            .select_from(UsageLedgerModel)
            .where(UsageLedgerModel.usage_date < date.today() - timedelta(days=60))
        ),
    }
    if preview:
        payload["cleanup_preview"] = {
            "expired_references": [{"version_id": i, "path": p} for i, p in references],
            "storage_expired_paths": storage_paths,
            "storage_orphan_paths": orphans if storage_paths is not None else None,
            "candidate_paths": eligible,
            "protected_recent_reference_paths": protected,
            "confirmed_candidate_paths": sorted(set(eligible) & set(storage_paths or []))
            if storage_paths is not None
            else None,
            "bucket": settings.raw_storage_bucket,
            "approval_required": True,
            "restore_requirement": "Verified object backup and version-to-path mapping before any deletion",
        }
    return payload


def relation_sizes(session):
    relations = None
    relation_error = None
    try:
        with session.begin_nested():
            relations = [
                dict(row)
                for row in session.execute(
                    text("""
                SELECT n.nspname AS schema, c.relname AS relation,
                       pg_table_size(c.oid) AS table_bytes,
                       pg_indexes_size(c.oid) AS index_bytes,
                       pg_total_relation_size(c.oid) AS total_bytes,
                       s.n_live_tup AS estimated_live_rows, s.n_dead_tup AS estimated_dead_rows,
                       s.last_autovacuum, s.last_autoanalyze
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
                WHERE c.relkind IN ('r','m') AND n.nspname NOT IN ('pg_catalog','information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 20
            """)
                ).mappings()
            ]
    except SQLAlchemyError as exc:
        relation_error = {
            "error_type": type(exc).__name__,
            "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
        }
    return relations, relation_error
