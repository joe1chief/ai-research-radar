"""Read-only historical payload estimates, never a compression/deletion executor."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Column, MetaData, Table, Text, case, cast, func, inspect, select, text
from sqlalchemy.types import LargeBinary

from .db import EventItemModel, ItemModel, ItemVersionModel


class RetentionInspectionUnavailable(RuntimeError):
    pass


def _evidence_table(session):
    connection = session.connection()
    inspector = inspect(connection)
    postgres = connection.dialect.name == "postgresql"
    schema = "public" if postgres else None
    present = inspector.has_table("evidence", schema=schema)
    if postgres and not present:
        raise RetentionInspectionUnavailable("Production evidence table is required")
    if present and "item_version_id" not in {
        c["name"] for c in inspector.get_columns("evidence", schema=schema)
    }:
        raise RetentionInspectionUnavailable("Evidence schema is unsupported")
    if postgres:
        refs = session.execute(
            text("""
            SELECT n.nspname AS schema_name, c.relname AS table_name,
                   a.attname AS source_column, target.attname AS target_column,
                   cardinality(k.conkey) AS key_columns
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=k.conkey[1]
            JOIN pg_attribute target ON target.attrelid=k.confrelid AND target.attnum=k.confkey[1]
            WHERE k.contype='f' AND k.confrelid='public.item_versions'::regclass
        """)
        ).all()
        if any(
            r.schema_name != "public"
            or r.table_name not in {"event_items", "evidence"}
            or r.source_column != "item_version_id"
            or r.target_column != "id"
            or r.key_columns != 1
            for r in refs
        ):
            raise RetentionInspectionUnavailable("Unrecognized version references")
    else:
        for name in inspector.get_table_names():
            for fk in inspector.get_foreign_keys(name):
                if fk["referred_table"] == "item_versions" and (
                    name not in {"event_items", "evidence"}
                    or fk["constrained_columns"] != ["item_version_id"]
                    or fk["referred_columns"] != ["id"]
                ):
                    raise RetentionInspectionUnavailable("Unrecognized version references")
    if not present:
        return None
    return Table("evidence", MetaData(), Column("item_version_id", Text), schema=schema)


def estimate_retention(session, *, preview=False, limit=200, now=None):
    """Count/estimate all candidates; expose only bounded IDs, never payloads or paths.

    Must be called inside readonly_session. Query failures propagate to the CLI's
    savepoint, which reports unavailable rather than assuming missing evidence.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("preview limit must be between 1 and 1000")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=90)
    evidence = _evidence_table(session)
    v, i = ItemVersionModel, ItemModel
    current = v.content_hash == i.current_content_hash
    missing_current = session.scalar(
        select(func.count())
        .select_from(i)
        .where(
            ~select(v.id)
            .where(v.item_id == i.id, v.content_hash == i.current_content_hash)
            .exists()
        )
    )
    if missing_current:
        raise RetentionInspectionUnavailable("Missing current version invariant")
    postgres = session.get_bind().dialect.name == "postgresql"
    if postgres:
        dimensions = session.scalar(
            text("""
            SELECT CASE WHEN t.typname='vector' THEN a.atttypmod ELSE NULL END
            FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
            WHERE attrelid='public.item_versions'::regclass AND attname='embedding_vector'
              AND attnum>0 AND NOT attisdropped
        """)
        )
        if dimensions != 1024:
            raise RetentionInspectionUnavailable("Unsupported vector dimensions/schema")
        text_size = lambda column: func.coalesce(func.octet_length(column), 0)
        array_size = func.coalesce(func.cardinality(v.embedding), 0) * 4
        vector_size = case((v.embedding_vector.is_not(None), 1024 * 4), else_=0)
    else:
        text_size = lambda column: func.coalesce(func.length(cast(column, LargeBinary)), 0)
        array_size = func.coalesce(func.json_array_length(v.embedding), 0) * 4
        vector_size = func.coalesce(func.json_array_length(v.embedding_vector), 0) * 4
    event_reference = (
        select(EventItemModel.event_id).where(EventItemModel.item_version_id == v.id).exists()
    )
    evidence_reference = (
        select(evidence.c.item_version_id).where(evidence.c.item_version_id == v.id).exists()
        if evidence is not None
        else case((current, False), else_=False)
    )
    query = (
        select(
            v.id,
            v.item_id,
            v.fetched_at,
            current.label("is_current"),
            # Rank non-current versions before current, so rank 1 is the latest predecessor.
            func.row_number()
            .over(
                partition_by=v.item_id,
                order_by=(case((current, 1), else_=0), v.fetched_at.desc(), v.id.desc()),
            )
            .label("previous_rank"),
            event_reference.label("event_reference"),
            evidence_reference.label("evidence_reference"),
            text_size(v.normalized_text).label("body_bytes"),
            text_size(v.abstract_text).label("abstract_bytes"),
            array_size.label("embedding_bytes"),
            vector_size.label("vector_bytes"),
        )
        .join(i, i.id == v.item_id)
        .order_by(v.item_id, v.id)
    )
    reasons = {
        name: 0
        for name in [
            "current",
            "latest_history",
            "event_reference",
            "evidence_reference",
            "within_90_days",
            "candidate",
        ]
    }
    age_buckets = {"within_30_days": 0, "31_to_90_days": 0, "over_90_days": 0}
    version_counts = {}
    candidate_count = 0
    bytes_by_column = {
        name: 0 for name in ["body_bytes", "abstract_bytes", "embedding_bytes", "vector_bytes"]
    }
    candidates = []
    total = 0
    for row in session.execute(query.execution_options(yield_per=1000)).mappings():
        total += 1
        version_counts[row["item_id"]] = version_counts.get(row["item_id"], 0) + 1
        fetched = row["fetched_at"]
        fetched = fetched.replace(tzinfo=UTC) if fetched.tzinfo is None else fetched
        age = (now - fetched).total_seconds() / 86400
        age_buckets[
            "within_30_days" if age <= 30 else "31_to_90_days" if age <= 90 else "over_90_days"
        ] += 1
        keep = []
        if row["is_current"]:
            keep.append("current")
        elif row["previous_rank"] == 1:
            keep.append("latest_history")
        if row["event_reference"]:
            keep.append("event_reference")
        if row["evidence_reference"]:
            keep.append("evidence_reference")
        if fetched >= cutoff:
            keep.append("within_90_days")
        for reason in keep:
            reasons[reason] += 1
        if keep:
            continue
        candidate_count += 1
        reasons["candidate"] += 1
        for name in bytes_by_column:
            bytes_by_column[name] += row[name]
        if preview and len(candidates) < limit:
            candidates.append(
                {
                    "version_id": row["id"],
                    "item_id": row["item_id"],
                    "fetched_at": fetched,
                    "age_days": round(age, 2),
                    "reason": "over_90_days_unreferenced_not_current_or_latest_history",
                    "logical_payload_bytes": sum(row[name] for name in bytes_by_column),
                }
            )
    histogram = {"one": 0, "two": 0, "three_or_more": 0}
    for count in version_counts.values():
        histogram["one" if count == 1 else "two" if count == 2 else "three_or_more"] += 1
    result = {
        "available": True,
        "read_only": True,
        "retention_days": 90,
        "cutoff_exclusive": cutoff,
        "versions": total,
        "items_by_version_count": histogram,
        "age_buckets": age_buckets,
        "retention_reason_counts": reasons,
        "reason_counts_overlap": True,
        "evidence_table_present": evidence is not None,
        "candidate_count": candidate_count,
        "candidate_logical_bytes_by_column": bytes_by_column,
        "candidate_logical_payload_bytes": sum(bytes_by_column.values()),
        "physical_reclaimable_bytes": None,
        "byte_basis": "UTF-8 text bytes plus 4 bytes per float32 component; excludes headers/indexes/TOAST",
        "preserve": "items and all version rows/IDs/hashes/titles/times/metadata; all event/delivery/webhook rows",
        "apply_available": False,
        "approval_required": True,
    }
    if preview:
        result.update(
            candidates=candidates,
            preview_limit=limit,
            preview_truncated=candidate_count > len(candidates),
        )
    return result
