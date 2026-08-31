"""One SQLAlchemy schema for local SQLite and the canonical Supabase/Postgres migration."""

from __future__ import annotations

from contextlib import contextmanager
from difflib import SequenceMatcher
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    ARRAY,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    REAL,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator
from pgvector.sqlalchemy import Vector

from .contracts import CollectedItem, EventStatus, SourceSpec
from .identity import content_hash, normalize_content, stable_id


def utcnow() -> datetime:
    return datetime.now(UTC)


class StringArray(TypeDecorator[list[str]]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Text()))
        return dialect.type_descriptor(JSON())


class FloatArray(TypeDecorator[list[float]]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(REAL()))
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    pass


class IssuerMasterModel(Base):
    __tablename__ = "issuer_master"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name_zh: Mapped[str | None] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(StringArray(), default=list)
    markets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    cik: Mapped[str | None] = mapped_column(Text)
    ir_url: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[str] = mapped_column(Text, default="extended")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceModel(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text)
    group: Mapped[str] = mapped_column("group_name", Text, index=True)
    kind: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    fetch_strategy: Mapped[str] = mapped_column(Text)
    cadence: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(Text)
    cursor_strategy: Mapped[str | None] = mapped_column(Text)
    parser: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceCursorModel(Base):
    __tablename__ = "source_cursors"

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_seen_native_id: Mapped[str | None] = mapped_column(Text)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceHealthModel(Base):
    __tablename__ = "source_health"

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    status: Mapped[str] = mapped_column(Text, default="unknown")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ItemModel(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("source_id", "native_id"),
        UniqueConstraint("source_id", "canonical_url"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    native_id: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    item_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ItemVersionModel(Base):
    __tablename__ = "item_versions"
    __table_args__ = (
        UniqueConstraint("item_id", "version_key"),
        UniqueConstraint("item_id", "content_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    version_key: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    abstract_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    raw_storage_path: Mapped[str | None] = mapped_column(Text)
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    embedding: Mapped[list[float] | None] = mapped_column(FloatArray())
    embedding_vector: Mapped[list[float] | None] = mapped_column(Vector(1024))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class RadarEventModel(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    cluster_id: Mapped[str] = mapped_column(Text, index=True)
    event_type: Mapped[str] = mapped_column(Text, index=True)
    topics: Mapped[list[str]] = mapped_column(StringArray(), default=list)
    entities: Mapped[list[str]] = mapped_column(StringArray(), default=list)
    cross_tags: Mapped[list[str]] = mapped_column(StringArray(), default=list)
    title_zh: Mapped[str] = mapped_column(Text)
    summary_zh: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text)
    change_summary: Mapped[str | None] = mapped_column(Text)
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    material_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, index=True)
    source_type: Mapped[str] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(Text, index=True)
    score: Mapped[int] = mapped_column(SmallInteger, index=True)
    primary_url: Mapped[str] = mapped_column(Text)
    corroborating_urls: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    delivery_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventRevisionModel(Base):
    __tablename__ = "event_revisions"
    __table_args__ = (
        UniqueConstraint("event_id", "revision_no"),
        UniqueConstraint("event_id", "content_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(Text)
    is_material: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventItemModel(Base):
    __tablename__ = "event_items"

    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    item_version_id: Mapped[str] = mapped_column(
        ForeignKey("item_versions.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(Text, default="primary")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryModel(Base):
    __tablename__ = "deliveries"

    delivery_key: Mapped[str] = mapped_column(Text, primary_key=True)
    recipient_hash: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(Text, default="agentmail")
    delivery_kind: Mapped[str] = mapped_column(Text)
    send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(Text, default="pending", index=True)
    agentmail_draft_id: Mapped[str | None] = mapped_column(Text)
    agentmail_message_id: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryEventRevisionModel(Base):
    __tablename__ = "delivery_event_revisions"

    delivery_key: Mapped[str] = mapped_column(
        ForeignKey("deliveries.delivery_key", ondelete="CASCADE"), primary_key=True
    )
    event_revision_id: Mapped[str] = mapped_column(
        ForeignKey("event_revisions.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookEventModel(Base):
    """AgentMail webhook ledger mirrored from the Supabase migration.

    Keeping this table in the portable ORM lets the 14:07 reconciliation job
    attach webhooks that arrived before a scheduled Draft's message ID became
    known locally.
    """

    __tablename__ = "webhook_events"

    provider_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, default="agentmail")
    event_type: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text, index=True)
    delivery_key: Mapped[str | None] = mapped_column(
        ForeignKey("deliveries.delivery_key", ondelete="SET NULL")
    )
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)


class UsageLedgerModel(Base):
    __tablename__ = "usage_ledger"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    usage_key: Mapped[str] = mapped_column(Text, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
    hard_limit: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    if database_url.startswith("sqlite:///"):
        raw_path = database_url.removeprefix("sqlite:///")
        if raw_path not in ("", ":memory:"):
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    is_sqlite = database_url.startswith("sqlite")
    args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(
        database_url,
        echo=echo,
        future=True,
        connect_args=args,
        # A collection run releases its connection between sources. Validate a
        # pooled PostgreSQL connection before the next source checks it out so
        # a pooler-side idle disconnect is replaced before a transaction starts.
        pool_pre_ping=not is_sqlite,
    )
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(connection: Any, _: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "Refusing ORM create_all outside SQLite; apply the checked-in Supabase migration first"
        )
    Base.metadata.create_all(engine)


PRODUCTION_TABLES = {
    "issuer_master",
    "sources",
    "source_cursors",
    "items",
    "item_versions",
    "events",
    "event_revisions",
    "event_items",
    "deliveries",
    "delivery_event_revisions",
    "webhook_events",
    "source_health",
    "usage_ledger",
}


def validate_production_schema(engine: Engine) -> None:
    """Fail closed unless the Supabase migration and RLS boundary are present."""

    if engine.dialect.name == "sqlite":
        return
    inspector = inspect(engine)
    missing = sorted(
        table for table in PRODUCTION_TABLES if not inspector.has_table(table, schema="public")
    )
    if missing:
        raise RuntimeError(
            "Production schema is not migrated; missing tables: " + ", ".join(missing)
        )
    without_rls: list[str] = []
    with engine.connect() as connection:
        for table in sorted(PRODUCTION_TABLES):
            enabled = connection.scalar(
                text(
                    "select c.relrowsecurity from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'public' and c.relname = :table"
                ),
                {"table": table},
            )
            if enabled is not True:
                without_rls.append(table)
    if without_rls:
        raise RuntimeError(
            "Production schema is unsafe because RLS is disabled for: "
            + ", ".join(without_rls)
        )
    item_version_columns = {
        column["name"]
        for column in inspector.get_columns("item_versions", schema="public")
    }
    if "embedding_vector" not in item_version_columns:
        raise RuntimeError("Production schema is missing item_versions.embedding_vector")
    event_columns = {
        column["name"] for column in inspector.get_columns("events", schema="public")
    }
    if "delivery_suppressed" not in event_columns:
        raise RuntimeError("Production schema is missing events.delivery_suppressed")


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def reserve_daily_usage(
    session: Session,
    *,
    usage_date: date,
    usage_key: str,
    hard_limit: int,
    amount: int = 1,
) -> bool:
    """Reserve a persistent daily quota before making an external API call."""

    if hard_limit <= 0 or amount <= 0:
        return False
    row = session.get(UsageLedgerModel, (usage_date, usage_key), with_for_update=True)
    if row is None:
        row = UsageLedgerModel(
            usage_date=usage_date,
            usage_key=usage_key,
            used=0,
            hard_limit=hard_limit,
            updated_at=utcnow(),
        )
        session.add(row)
        session.flush()
    if row.used + amount > hard_limit:
        row.hard_limit = max(row.hard_limit, row.used)
        return False
    row.hard_limit = hard_limit
    row.used += amount
    row.updated_at = utcnow()
    session.flush()
    return True


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_source(session: Session, spec: SourceSpec) -> SourceModel:
    row = session.get(SourceModel, spec.id)
    if row is None:
        row = SourceModel(id=spec.id, created_at=utcnow())
        session.add(row)
    values = spec.model_dump()
    for field in (
        "entity_id",
        "group",
        "kind",
        "url",
        "fetch_strategy",
        "cadence",
        "evidence_type",
        "cursor_strategy",
        "parser",
        "enabled",
    ):
        setattr(row, field, values[field])
    row.config = {key: value for key, value in values.items() if key not in row.__mapper__.attrs.keys()}
    row.updated_at = utcnow()
    session.flush()
    return row


def sync_issuers(session: Session, issuers: list[dict[str, Any]]) -> int:
    """Upsert the version-controlled issuer master into local/production storage."""

    for value in issuers:
        row = session.get(IssuerMasterModel, value["id"])
        if row is None:
            row = IssuerMasterModel(id=value["id"], created_at=utcnow())
            session.add(row)
        row.name_zh = value.get("name_zh")
        row.name_en = value.get("name_en")
        row.aliases = list(value.get("aliases", []))
        row.markets = list(value.get("markets", []))
        row.cik = value.get("cik")
        row.ir_url = value.get("ir_url")
        row.is_private = bool(value.get("private", False))
        row.priority = value.get("priority", "extended")
        row.metadata_json = {
            key: item
            for key, item in value.items()
            if key
            not in {
                "id",
                "name_zh",
                "name_en",
                "aliases",
                "markets",
                "cik",
                "ir_url",
                "private",
                "priority",
            }
        }
        row.updated_at = utcnow()
    session.flush()
    return len(issuers)


def ensure_cursor(session: Session, source_id: str) -> SourceCursorModel:
    row = session.get(SourceCursorModel, source_id)
    if row is None:
        row = SourceCursorModel(source_id=source_id, cursor={}, updated_at=utcnow())
        session.add(row)
        session.flush()
    return row


def ensure_source_health(session: Session, source_id: str) -> SourceHealthModel:
    row = session.get(SourceHealthModel, source_id)
    if row is None:
        row = SourceHealthModel(source_id=source_id, status="unknown", metadata_json={})
        session.add(row)
        session.flush()
    return row


def _collected_hash(item: CollectedItem) -> str:
    revision_identity = {
        key: item.metadata.get(key)
        for key in (
            "version",
            "tag_name",
            "sha",
            "revision",
            "accession_number",
            "document_id",
            "acceptance_status",
            "code_url",
            "project_url",
        )
        if item.metadata.get(key) is not None
    }
    return content_hash(
        "\n".join(
            [
                item.title,
                item.summary,
                item.content,
                item.canonical_url,
                json.dumps(revision_identity, ensure_ascii=False, sort_keys=True),
            ]
        )
    )


def current_item_version(session: Session, item: ItemModel) -> ItemVersionModel:
    row = session.scalar(
        select(ItemVersionModel).where(
            ItemVersionModel.item_id == item.id,
            ItemVersionModel.content_hash == item.current_content_hash,
        )
    )
    if row is None:
        raise RuntimeError(f"item {item.id} has no current version")
    return row


def ingest_item(session: Session, spec: SourceSpec, item: CollectedItem) -> tuple[ItemModel, bool]:
    digest = _collected_hash(item)
    arxiv_kind = spec.kind in {"arxiv_api", "arxiv_oai"}
    if arxiv_kind:
        row = session.scalar(
            select(ItemModel).where(
                ItemModel.native_id == item.external_id,
                ItemModel.item_type.in_(["arxiv_api", "arxiv_oai"]),
            )
        )
    else:
        # Feeds occasionally rotate GUIDs for the same canonical article. The
        # URL identity is therefore an equal fallback within one source.
        row = session.scalar(
            select(ItemModel).where(
                ItemModel.source_id == spec.id,
                or_(
                    ItemModel.native_id == item.external_id,
                    ItemModel.canonical_url == item.canonical_url,
                ),
            )
        )
    now = utcnow()
    is_new = row is None
    previous_metadata: dict[str, Any] = {}
    if row is None:
        target_id = stable_id("arxiv" if arxiv_kind else spec.id, item.external_id)
        row = session.get(ItemModel, target_id)
    if row is None:
        row = ItemModel(
            id=stable_id("arxiv" if arxiv_kind else spec.id, item.external_id),
            source_id=spec.id,
            native_id=item.external_id,
            canonical_url=item.canonical_url,
            item_type=spec.kind,
            entity_id=item.entity_id or spec.entity_id,
            title=normalize_content(item.title),
            published_at=item.published_at,
            source_updated_at=item.updated_at,
            first_seen_at=now,
            last_seen_at=now,
            current_content_hash=digest,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        published = item.published_at
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        status = (
            EventStatus.DISCOVERED_LATE
            if published is not None and published < now - timedelta(hours=48)
            else EventStatus.NEW_ENTITY
        )
        revision_no = 1
        change_details = (
            "来源时间早于采集窗口，本次首次发现"
            if status == EventStatus.DISCOVERED_LATE
            else "首次收录"
        )
    else:
        row.last_seen_at = now
        if row.current_content_hash == digest:
            return row, False
        previous_metadata = dict(row.metadata_json or {})
        try:
            previous_version = current_item_version(session, row)
        except RuntimeError:
            previous_version = None
        old_source_metadata = previous_metadata.get("source_metadata", {})
        old_version = old_source_metadata.get("version")
        new_version = item.metadata.get("version")
        artifact_changed = any(
            old_source_metadata.get(key) != item.metadata.get(key)
            for key in ("acceptance_status", "code_url", "project_url")
            if old_source_metadata.get(key) is not None or item.metadata.get(key) is not None
        )
        material = (
            (arxiv_kind and old_version != new_version)
            or artifact_changed
            or normalize_content(row.title) != normalize_content(item.title)
            or _substantive_text_change(
                str(previous_metadata.get("last_summary") or ""),
                item.summary,
            )
            or _substantive_body_change(
                previous_version.normalized_text if previous_version else "",
                item.content or item.summary,
                kind=spec.kind,
            )
        )
        status = EventStatus.MATERIAL_UPDATE if material else EventStatus.MINOR_UPDATE
        change_details = _describe_change(
            previous_title=row.title,
            current_title=item.title,
            previous_summary=str(previous_metadata.get("last_summary") or ""),
            current_summary=item.summary,
            previous_body=previous_version.normalized_text if previous_version else "",
            current_body=item.content or item.summary,
            previous_metadata=old_source_metadata,
            current_metadata=item.metadata,
            status=status,
        )
        revision_no = int(previous_metadata.get("revision_no", 1)) + 1
        row.canonical_url = item.canonical_url
        row.title = normalize_content(item.title)
        row.published_at = item.published_at
        row.source_updated_at = item.updated_at
        row.current_content_hash = digest
        row.updated_at = now

    native_version = item.metadata.get("version") or item.metadata.get("tag_name") or "content"
    version_key = f"{native_version}:{digest[:16]}"
    version_id = stable_id(row.id, digest)
    version = session.get(ItemVersionModel, version_id)
    if version is None:
        version = session.scalar(
            select(ItemVersionModel).where(
                ItemVersionModel.item_id == row.id,
                ItemVersionModel.content_hash == digest,
            )
        )
    if version is None:
        version = session.scalar(
            select(ItemVersionModel).where(
                ItemVersionModel.item_id == row.id,
                ItemVersionModel.version_key == version_key,
            )
        )
    if version is None:
        version = ItemVersionModel(
            id=version_id,
            item_id=row.id,
            version_key=version_key,
            content_hash=digest,
            title=normalize_content(item.title),
            abstract_text=normalize_content(item.summary),
            normalized_text=normalize_content(item.content or item.summary),
            source_time=item.updated_at or item.published_at,
            fetched_at=now,
            metadata_json={
                **item.metadata,
                "evidence_type": item.evidence_type or spec.evidence_type,
            },
        )
        session.add(version)
    else:
        version.title = normalize_content(item.title)
        version.abstract_text = normalize_content(item.summary)
        version.normalized_text = normalize_content(item.content or item.summary)
        version.source_time = item.updated_at or item.published_at
        version.fetched_at = now
        version.metadata_json = {
            **(version.metadata_json or {}),
            **item.metadata,
            "evidence_type": item.evidence_type or spec.evidence_type,
        }
    row.metadata_json = {
        **previous_metadata,
        "evidence_type": item.evidence_type or spec.evidence_type,
        "source_metadata": item.metadata,
        "last_summary": normalize_content(item.summary),
        "update_status": status.value,
        "change_summary": change_details,
        "revision_no": revision_no,
        "processed_hash": previous_metadata.get("processed_hash"),
    }
    session.flush()
    return row, True


def _describe_change(
    *,
    previous_title: str,
    current_title: str,
    previous_summary: str,
    current_summary: str,
    previous_body: str,
    current_body: str,
    previous_metadata: dict[str, Any],
    current_metadata: dict[str, Any],
    status: EventStatus,
) -> str:
    if status == EventStatus.MINOR_UPDATE:
        return "仅页面结构、链接参数或非关键元数据变化"
    changes: list[str] = []
    old_version = previous_metadata.get("version") or previous_metadata.get("tag_name")
    new_version = current_metadata.get("version") or current_metadata.get("tag_name")
    if old_version != new_version and (old_version is not None or new_version is not None):
        changes.append(f"版本 {old_version or '无'}→{new_version or '无'}")
    for key, label in (
        ("acceptance_status", "录用状态"),
        ("code_url", "代码链接"),
        ("project_url", "项目页"),
        ("amount", "金额"),
        ("participants", "参与方"),
        ("transaction_status", "交易状态"),
        ("regulatory_outcome", "监管结论"),
    ):
        before = previous_metadata.get(key)
        after = current_metadata.get(key)
        if before != after and (before is not None or after is not None):
            changes.append(f"{label} {_short_value(before)}→{_short_value(after)}")
    old_title = normalize_content(previous_title)
    new_title = normalize_content(current_title)
    if old_title != new_title:
        changes.append(f"标题“{old_title[:60]}”→“{new_title[:60]}”")
    old_summary = normalize_content(previous_summary)
    new_summary = normalize_content(current_summary)
    if _substantive_text_change(old_summary, new_summary):
        changes.append(f"核心摘要 {_changed_excerpt(old_summary, new_summary)}")
    elif _substantive_body_change(previous_body, current_body, kind="html"):
        changes.append(
            f"正文实质更新（{len(normalize_content(previous_body))}→"
            f"{len(normalize_content(current_body))} 字）"
        )
    return "；".join(changes[:4]) or "关键事实发生实质变化"


def _short_value(value: Any) -> str:
    if value is None or value == "":
        return "无"
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value[:5])
    return normalize_content(str(value))[:80] or "无"


def _changed_excerpt(previous: str, current: str) -> str:
    matcher = SequenceMatcher(None, previous, current, autojunk=False)
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        before = previous[left_start:left_end].strip()[:80] or "无"
        after = current[right_start:right_end].strip()[:80] or "无"
        return f"“{before}”→“{after}”"
    return "发生变化"


def _substantive_body_change(previous: str, current: str, *, kind: str) -> bool:
    """Separate article/release edits from whitespace, chrome and timestamp churn."""

    if kind not in {"html", "rss", "github_releases"}:
        return False
    left = normalize_content(previous)
    right = normalize_content(current)
    if left == right or not left or not right:
        return False
    length_delta = abs(len(left) - len(right)) / max(len(left), len(right), 1)
    similarity = SequenceMatcher(None, left[:20000], right[:20000], autojunk=False).ratio()
    return length_delta >= 0.08 or similarity < 0.94


def _substantive_text_change(previous: str, current: str) -> bool:
    left = normalize_content(previous)
    right = normalize_content(current)
    if left == right:
        return False
    if not left or not right:
        return bool(left or right)
    length_delta = abs(len(left) - len(right)) / max(len(left), len(right), 1)
    similarity = SequenceMatcher(None, left[:10000], right[:10000], autojunk=False).ratio()
    return length_delta >= 0.08 or similarity < 0.94
