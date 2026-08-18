"""The ``radar`` operational command line."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import typer
from sqlalchemy import delete, func, select, text
from zoneinfo import ZoneInfo

from .alphaxiv import MCPAlphaXivAdapter, enrich_alphaxiv_top
from .agentmail import AgentMailClient, deliver_outbox, reconcile_drafts
from .compose import compose_delivery, ensure_operations_delivery
from .config import load_issuers, load_sources
from .db import (
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    SourceCursorModel,
    SourceHealthModel,
    UsageLedgerModel,
    create_db_engine,
    init_schema,
    session_factory,
    session_scope,
    sync_issuers,
    sync_source,
    validate_production_schema,
)
from .evaluation import evaluate_topic_labels, load_topic_labels
from .exporter import export_public_dataset
from .llm import QwenClient
from .pipeline import (
    collect_group,
    editorialize_top,
    enrich_pending,
    recover_pending_embeddings,
)
from .raw_storage import RawSnapshotStore
from .settings import Settings, get_settings
from .topics import RuleTopicClassifier

app = typer.Typer(help="Incremental AI research and industry intelligence radar.", no_args_is_help=True)


class SourceGroup(StrEnum):
    PAPERS = "papers"
    TECH = "tech"
    CAPITAL = "capital"
    STANDARDS = "standards"


class DeliveryKind(StrEnum):
    DIGEST = "digest"
    ALERT = "alert"


def _runtime() -> tuple[Settings, object]:
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    if settings.app_env == "production" and engine.dialect.name != "postgresql":
        raise RuntimeError("APP_ENV=production requires a PostgreSQL/Supabase database")
    if engine.dialect.name == "sqlite":
        init_schema(engine)
    else:
        validate_production_schema(engine)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        sync_issuers(session, load_issuers(settings.config_dir))
    return settings, factory


def _print(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


def _raw_store(settings: Settings) -> RawSnapshotStore | None:
    if not settings.supabase_url or not settings.supabase_secret_key:
        return None
    return RawSnapshotStore(
        supabase_url=settings.supabase_url,
        secret_key=settings.supabase_secret_key,
        bucket=settings.raw_storage_bucket,
        max_bytes=settings.raw_snapshot_max_bytes,
    )


@app.command("init-db")
def init_db() -> None:
    """Create the portable schema (Supabase uses the checked-in SQL migration)."""

    settings, _ = _runtime()
    _print({"database": settings.database_url, "initialized": True})


@app.command()
def collect(
    group: SourceGroup = typer.Option(..., "--group"),
    force: bool = typer.Option(False, "--force", help="Ignore cadence for manual recovery"),
) -> None:
    settings, factory = _runtime()
    sources = load_sources(settings.config_dir)
    raw_store = _raw_store(settings)
    try:
        with session_scope(factory) as session:
            stats = collect_group(
                session,
                sources,
                group=group.value,
                user_agent=settings.user_agent,
                force=force,
                github_token=settings.github_token,
                openreview_access_token=settings.openreview_access_token,
                raw_store=raw_store,
            )
            _print(stats.to_dict())
    finally:
        if raw_store is not None:
            raw_store.close()
    if stats.failed:
        raise typer.Exit(code=1)


@app.command()
def enrich(
    limit: int = typer.Option(300, min=1, max=1000),
    summary_limit: int | None = typer.Option(None, min=0, max=100, help="Qwen Plus card cap"),
) -> None:
    settings, factory = _runtime()
    qwen = QwenClient(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        classifier_model=settings.classifier_model,
        summarizer_model=settings.summarizer_model,
        embedding_model=settings.embedding_model,
    )
    try:
        with session_scope(factory) as session:
            resolved_summary_limit = (
                settings.daily_summary_limit if summary_limit is None else summary_limit
            )
            result = enrich_pending(
                session,
                classifier=RuleTopicClassifier.from_config(settings.config_dir),
                qwen=qwen,
                config_dir=settings.config_dir,
                limit=min(limit, settings.daily_classify_limit),
                timezone=settings.timezone,
                daily_qwen_limit=settings.daily_classify_limit,
            )
            result["embedding_recovery"] = recover_pending_embeddings(
                session,
                qwen=qwen,
                limit=settings.daily_reembed_limit,
                timezone=settings.timezone,
                daily_limit=settings.daily_reembed_limit,
            )
            if settings.alphaxiv_access_token:
                alphaxiv = MCPAlphaXivAdapter(
                    access_token=settings.alphaxiv_access_token,
                    endpoint=settings.alphaxiv_mcp_url,
                )
                try:
                    result["alphaxiv"] = enrich_alphaxiv_top(
                        session,
                        alphaxiv,
                        limit=settings.alphaxiv_daily_read_limit,
                        timezone=settings.timezone,
                        daily_limit=settings.alphaxiv_daily_read_limit,
                    )
                finally:
                    alphaxiv.close()
            else:
                result["alphaxiv"] = {"attempted": 0, "enriched": 0, "failed": 0}
            result["qwen_plus"] = editorialize_top(
                session,
                qwen=qwen,
                limit=resolved_summary_limit,
                timezone=settings.timezone,
                daily_limit=settings.daily_summary_limit,
            )
            _print({**result, "qwen_enabled": qwen.enabled})
    finally:
        qwen.close()


@app.command()
def compose(
    digest_date: str = typer.Option(date.today().isoformat(), "--date"),
    kind: DeliveryKind = typer.Option(DeliveryKind.DIGEST, "--kind"),
) -> None:
    settings, factory = _runtime()
    target = date.fromisoformat(digest_date)
    if settings.delivery_mode == "live" and not settings.digest_recipient:
        raise typer.BadParameter("DIGEST_RECIPIENT is required in live mode")
    with session_scope(factory) as session:
        row = compose_delivery(
            session,
            digest_date=target,
            recipient=settings.digest_recipient,
            timezone=settings.timezone,
            kind=kind.value,
        )
        if row is None:
            _print({"delivery_key": None, "state": "no_new_alerts", "send_at": None})
        else:
            _print({"delivery_key": row.delivery_key, "state": row.state, "send_at": row.send_at})


@app.command()
def deliver() -> None:
    settings, factory = _runtime()
    client = None
    if settings.agentmail_api_key and settings.agentmail_inbox_id:
        client = AgentMailClient(
            api_key=settings.agentmail_api_key,
            inbox_id=settings.agentmail_inbox_id,
        )
    elif settings.delivery_mode == "live" and not settings.dry_run:
        if not settings.agentmail_api_key or not settings.agentmail_inbox_id:
            raise typer.BadParameter("AGENTMAIL_API_KEY and AGENTMAIL_INBOX_ID are required")
    with session_scope(factory) as session:
        result = deliver_outbox(
            session,
            mode="shadow" if settings.dry_run else settings.delivery_mode,
            recipient=settings.digest_recipient,
            client=client,
        )
    _print(result)
    if result["failed"] or result["unknown"]:
        raise typer.Exit(code=1)


@app.command()
def reconcile() -> None:
    settings, factory = _runtime()
    client = None
    if settings.agentmail_api_key and settings.agentmail_inbox_id:
        client = AgentMailClient(
            api_key=settings.agentmail_api_key,
            inbox_id=settings.agentmail_inbox_id,
        )
    with session_scope(factory) as session:
        mode = "shadow" if settings.dry_run else settings.delivery_mode
        recovered = deliver_outbox(
            session,
            mode=mode,
            recipient=settings.digest_recipient,
            client=client,
        )
        reconciled = reconcile_drafts(session, client)
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        operations = (
            ensure_operations_delivery(
                session,
                digest_date=today,
                recipient=settings.digest_recipient,
                timezone=settings.timezone,
            )
            if mode == "live"
            else None
        )
        operations_result = (
            deliver_outbox(
                session,
                mode=mode,
                recipient=settings.digest_recipient,
                client=client,
            )
            if operations is not None and operations.state == "pending"
            else None
        )
        payload = {
            "recovered_pending": recovered,
            "reconciled": reconciled,
            "operations_delivery": operations.delivery_key if operations else None,
            "operations_result": operations_result,
        }
    _print(payload)
    delivery_failed = any(
        int(result.get("failed", 0)) or int(result.get("unknown", 0))
        for result in (recovered, reconciled, operations_result or {})
    )
    if delivery_failed or operations is not None:
        raise typer.Exit(code=1)


@app.command()
def backfill(days: int = typer.Option(14, min=1, max=90)) -> None:
    """Backfill archives only; this command never composes or sends historical mail."""

    settings, factory = _runtime()
    sources = load_sources(settings.config_dir)
    expanded: list = []
    for source in sources:
        if source.kind == "arxiv_api":
            source = source.model_copy(
                update={"max_pages": max(20, min(200, days * 4))}
            )
        elif source.kind == "arxiv_oai":
            source = source.model_copy(update={"max_pages": 80})
        elif source.kind in {
            "sse_announcements",
            "cninfo_announcements",
            "sec_submissions",
        }:
            source = source.model_copy(update={"lookback_days": days})
        expanded.append(source)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    qwen = QwenClient(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        classifier_model=settings.classifier_model,
        summarizer_model=settings.summarizer_model,
        embedding_model=settings.embedding_model,
    )
    raw_store = _raw_store(settings)
    try:
        with session_scope(factory) as session:
            for source in expanded:
                sync_source(session, source)
                cursor = session.get(SourceCursorModel, source.id)
                if cursor is None:
                    cursor = SourceCursorModel(source_id=source.id, cursor={})
                    session.add(cursor)
                    session.flush()
                value = dict(cursor.cursor or {})
                # A backfill is an explicit replay and must never be converted
                # into a 304 by validators from an earlier incremental run.
                cursor.etag = None
                cursor.last_modified = None
                value.pop("etag", None)
                value.pop("last_modified", None)
                if source.kind == "arxiv_api":
                    value["updated_at"] = cutoff.isoformat()
                elif source.kind == "arxiv_oai":
                    value.update(
                        {
                            "oai_from": cutoff.date().isoformat(),
                            "set_index": 0,
                            "resumption_token": "",
                        }
                    )
                elif source.kind == "sec_submissions":
                    # A backfill intentionally replays the requested SEC date
                    # window even when normal collection already has a newer
                    # accession watermark. The collector immediately writes a
                    # fresh newest-accession cursor after this run.
                    cursor.last_seen_native_id = None
                    value.pop("last_seen_native_id", None)
                cursor.cursor = value
            collection = {}
            for group in SourceGroup:
                collection[group.value] = collect_group(
                    session,
                    expanded,
                    group=group.value,
                    user_agent=settings.user_agent,
                    force=True,
                    github_token=settings.github_token,
                    openreview_access_token=settings.openreview_access_token,
                    raw_store=raw_store,
                ).to_dict()
            enrichment_batches = []
            for _ in range(20):
                result = enrich_pending(
                    session,
                    classifier=RuleTopicClassifier.from_config(settings.config_dir),
                    qwen=qwen,
                    config_dir=settings.config_dir,
                    limit=300,
                    timezone=settings.timezone,
                    daily_qwen_limit=settings.daily_classify_limit,
                    suppress_delivery=True,
                    source_time_cutoff=cutoff,
                )
                enrichment_batches.append(result)
                if result["processed"] == 0:
                    break
            degraded_sources = [
                row.source_id
                for row in session.scalars(select(SourceHealthModel)).all()
                if row.status == "degraded"
            ]
            payload = {
                "days": days,
                "archive_only": True,
                "collection": collection,
                "degraded_sources": degraded_sources,
                "enrichment_batches": enrichment_batches,
            }
    finally:
        qwen.close()
        if raw_store is not None:
            raw_store.close()
    _print(payload)
    if any(
        int(stats.get("failed", 0)) or int(stats.get("degraded", 0))
        for stats in collection.values()
    ):
        raise typer.Exit(code=1)


@app.command("export-web")
def export_web(output: Path | None = typer.Option(None, "--output")) -> None:
    settings, factory = _runtime()
    target = output or settings.public_data_path
    with session_scope(factory) as session:
        dataset = export_public_dataset(
            session,
            target,
            config_dir=settings.config_dir,
            timezone=settings.timezone,
        )
        _print({"output": str(target), "events": len(dataset["events"])})


@app.command()
def maintenance() -> None:
    """Reconcile health, retention markers, quota history and capacity thresholds."""

    settings, factory = _runtime()
    raw_store = _raw_store(settings)
    try:
        with session_scope(factory) as session:
            cutoff = datetime.now(UTC) - timedelta(days=14)
            expired_raw = session.scalars(
                select(ItemVersionModel).where(
                    ItemVersionModel.raw_storage_path.is_not(None),
                    ItemVersionModel.fetched_at < cutoff,
                )
            ).all()
            expired_paths = [
                version.raw_storage_path
                for version in expired_raw
                if version.raw_storage_path
            ]
            removed_raw = 0
            if raw_store is not None:
                storage_expired = raw_store.list_older_than(cutoff.date())
                delete_paths = sorted(set([*expired_paths, *storage_expired]))
                raw_store.delete(delete_paths)
                for version in expired_raw:
                    version.raw_storage_path = None
                removed_raw = len(delete_paths)
            session.execute(
                delete(UsageLedgerModel).where(
                    UsageLedgerModel.usage_date < (date.today() - timedelta(days=60))
                )
            )
            health = session.scalars(select(SourceHealthModel)).all()
            engine = session.get_bind()
            if engine.dialect.name == "sqlite":
                path = settings.database_url.removeprefix("sqlite:///")
                database_bytes = Path(path).stat().st_size if path and Path(path).exists() else 0
            else:
                database_bytes = int(
                    session.scalar(text("select pg_database_size(current_database())")) or 0
                )
            capacity_warning = database_bytes >= 350 * 1024 * 1024
            payload = {
                "items": session.scalar(select(func.count()).select_from(ItemModel)),
                "versions": session.scalar(select(func.count()).select_from(ItemVersionModel)),
                "events": session.scalar(select(func.count()).select_from(RadarEventModel)),
                "expired_raw_objects_removed": removed_raw,
                "expired_raw_objects_pending": 0 if raw_store else len(expired_raw),
                "sources_failing": [
                    row.source_id for row in health if row.consecutive_failures >= 3
                ],
                "database_bytes": database_bytes,
                "capacity_warning_350mb": capacity_warning,
            }
    finally:
        if raw_store is not None:
            raw_store.close()
    _print(payload)
    if (
        payload["capacity_warning_350mb"]
        or payload["sources_failing"]
        or (settings.app_env == "production" and payload["expired_raw_objects_pending"])
    ):
        raise typer.Exit(code=1)


@app.command("evaluate-topics")
def evaluate_topics(
    dataset: Path = typer.Option(..., "--dataset", exists=True, dir_okay=False),
    minimum_samples: int = typer.Option(100, "--minimum-samples", min=1),
    threshold: float = typer.Option(0.85, min=0.0, max=1.0),
    rules_only: bool = typer.Option(
        False,
        "--rules-only",
        help="Offline diagnostic only; the production acceptance gate uses Qwen Flash.",
    ),
) -> None:
    """Run the fail-closed Top-1 gate over independently reviewed JSONL labels."""

    settings = get_settings()
    qwen = QwenClient(
        api_key=None if rules_only else settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        classifier_model=settings.classifier_model,
        summarizer_model=settings.summarizer_model,
        embedding_model=settings.embedding_model,
    )
    if not rules_only and not qwen.enabled:
        qwen.close()
        raise typer.BadParameter(
            "DASHSCOPE_API_KEY is required; use --rules-only only for offline diagnostics"
        )
    try:
        result = evaluate_topic_labels(
            load_topic_labels(dataset),
            RuleTopicClassifier.from_config(settings.config_dir),
            minimum_samples=minimum_samples,
            threshold=threshold,
            qwen=None if rules_only else qwen,
        )
    finally:
        qwen.close()
    _print(result)
    if not result["passed"]:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
