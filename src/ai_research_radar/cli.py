"""The ``radar`` operational command line."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from sqlalchemy import select
from zoneinfo import ZoneInfo

from .alphaxiv import MCPAlphaXivAdapter, enrich_alphaxiv_top
from .agentmail import AgentMailClient, deliver_outbox, reconcile_drafts
from .compose import compose_delivery, ensure_operations_delivery
from .config import load_issuers, load_sources
from .contracts import SourceSpec
from .db import (
    RadarEventModel,
    SourceHealthModel,
    create_db_engine,
    init_schema,
    session_factory,
    session_scope,
    sync_issuers,
    validate_production_schema,
)
from .evaluation import evaluate_topic_labels, load_topic_labels
from .exporter import export_public_dataset
from .llm import ModelClient, ModelSmokeError
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


@app.callback()
def configure_logging() -> None:
    # StreamHandler flushes each message; do not log requests, secrets or bodies.
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("ai_research_radar").setLevel(logging.INFO)
    # HTTPX INFO includes the full URL, including sensitive query parameters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class SourceGroup(StrEnum):
    PAPERS = "papers"
    TECH = "tech"
    CAPITAL = "capital"
    STANDARDS = "standards"


class DeliveryKind(StrEnum):
    DIGEST = "digest"
    ALERT = "alert"


def _backfill_cursor_payload(
    source: SourceSpec, cursor: dict[str, Any], cutoff: datetime
) -> dict[str, Any]:
    """Build an in-memory replay cursor without mutating production state."""

    replay = dict(cursor)
    # Explicit None values clear persisted validators when the successful
    # collector batch is applied; absent keys would preserve the old values.
    replay["etag"] = None
    replay["last_modified"] = None
    if source.kind == "arxiv_api":
        replay["updated_at"] = cutoff.isoformat()
    elif source.kind == "arxiv_oai":
        replay.update(
            {
                "oai_from": cutoff.date().isoformat(),
                "set_index": 0,
                "resumption_token": "",
            }
        )
    elif source.kind == "sec_submissions":
        replay["last_seen_native_id"] = None
    return replay


def _runtime() -> tuple[Settings, object]:
    settings = get_settings()
    logger = logging.getLogger(__name__)
    logger.info("runtime stage: stage=database_engine")
    engine = create_db_engine(
        settings.database_url,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
        statement_timeout_seconds=settings.db_statement_timeout_seconds,
        lock_timeout_seconds=settings.db_lock_timeout_seconds,
    )
    logger.info("runtime stage: stage=schema_check")
    if settings.app_env == "production" and engine.dialect.name != "postgresql":
        raise RuntimeError("APP_ENV=production requires a PostgreSQL/Supabase database")
    if engine.dialect.name == "sqlite":
        init_schema(engine)
    else:
        validate_production_schema(engine)
    logger.info("runtime stage: stage=issuer_sync")
    factory = session_factory(engine)
    with session_scope(factory) as session:
        sync_issuers(session, load_issuers(settings.config_dir))
    logger.info("runtime stage: stage=ready")
    return settings, factory


def _print(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


def _raw_store(settings: Settings) -> RawSnapshotStore | None:
    if (
        not settings.raw_storage_enabled
        or not settings.supabase_url
        or not settings.supabase_secret_key
    ):
        return None
    return RawSnapshotStore(
        supabase_url=settings.supabase_url,
        secret_key=settings.supabase_secret_key,
        bucket=settings.raw_storage_bucket,
        max_bytes=settings.raw_snapshot_max_bytes,
    )


def _model_client(settings: Settings, *, chat_enabled: bool = True) -> ModelClient:
    return ModelClient(
        api_key=settings.llm_api_key_value if chat_enabled else None,
        base_url=settings.llm_base_url,
        provider=settings.llm_provider,
        classifier_model=settings.classifier_model,
        summarizer_model=settings.summarizer_model,
        embedding_mode=settings.embedding_mode,
        embedding_api_key=settings.embedding_api_key_value,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        enable_thinking=settings.effective_enable_thinking,
        json_response_format=settings.llm_json_response_format,
        max_tokens=settings.llm_max_tokens,
    )


@app.command("init-db")
def init_db() -> None:
    """Create the portable schema (Supabase uses the checked-in SQL migration)."""

    settings, _ = _runtime()
    _print({"database": settings.database_url, "initialized": True})


@app.command("model-smoke")
def model_smoke() -> None:
    """Strictly validate the configured model provider before production use."""

    try:
        settings = get_settings()
    except ValidationError:
        _print(
            {
                "ok": False,
                "stage": "configuration",
                "error": "invalid_model_configuration",
            }
        )
        raise typer.Exit(code=2) from None
    client = _model_client(settings)
    try:
        try:
            result = client.smoke_test()
        except ModelSmokeError as exc:
            _print(
                {
                    "ok": False,
                    "provider": settings.llm_provider,
                    "stage": exc.stage,
                    "error": exc.code,
                }
            )
            raise typer.Exit(code=1) from None
        _print(
            {
                "ok": True,
                "provider": settings.llm_provider,
                "models": result.models_status,
                "models_visible": result.models_visible,
                "classifier_chat": "ok",
                "classifier_model": result.classifier_model,
                "summarizer_chat": "ok",
                "summarizer_model": result.summarizer_model,
                "embedding_mode": result.embedding_mode,
                "embedding": "ok" if result.embedding_checked else "local_intentional",
                "embedding_dimensions": result.embedding_dimensions,
            }
        )
    finally:
        client.close()


@app.command("agentmail-smoke")
def agentmail_smoke() -> None:
    """Validate configured AgentMail credentials and inbox connectivity."""

    try:
        settings = get_settings()
    except ValidationError:
        _print(
            {
                "ok": False,
                "stage": "configuration",
                "error": "invalid_configuration",
            }
        )
        raise typer.Exit(code=2) from None

    if not settings.agentmail_api_key or not settings.agentmail_inbox_id:
        _print(
            {
                "ok": False,
                "stage": "configuration",
                "error": "missing_agentmail_credentials",
                "has_api_key": bool(settings.agentmail_api_key),
                "has_inbox_id": bool(settings.agentmail_inbox_id),
            }
        )
        raise typer.Exit(code=2)

    try:
        from agentmail import AgentMail
        client = AgentMail(api_key=settings.agentmail_api_key)
        inbox = client.inboxes.get(inbox_id=settings.agentmail_inbox_id)
        inbox_data = inbox.model_dump() if hasattr(inbox, "model_dump") else dict(inbox)

        draft_test = None
        if settings.digest_recipient:
            try:
                test_client_id = f"smoke-{int(time.time())}"
                draft = client.inboxes.drafts.create(
                    inbox_id=settings.agentmail_inbox_id,
                    to=[settings.digest_recipient],
                    subject="AgentMail Smoke Test Draft",
                    text="This is an automated smoke test draft.",
                    html="<p>This is an automated smoke test draft.</p>",
                    client_id=test_client_id,
                )
                draft_id = getattr(draft, "draft_id", None) or (draft.get("draft_id") if isinstance(draft, dict) else None)
                draft_test = {"created": True, "draft_id": str(draft_id)}
                if draft_id:
                    try:
                        client.inboxes.drafts.delete(
                            inbox_id=settings.agentmail_inbox_id,
                            draft_id=str(draft_id),
                        )
                        draft_test["deleted"] = True
                    except Exception as del_exc:
                        draft_test["delete_error"] = str(del_exc)
            except Exception as draft_exc:
                draft_test = {
                    "created": False,
                    "error": str(draft_exc),
                    "status_code": getattr(draft_exc, "status_code", None),
                }

        _print(
            {
                "ok": True,
                "inbox_id": settings.agentmail_inbox_id,
                "inbox_name": inbox_data.get("name") or getattr(inbox, "name", None),
                "inbox_email": inbox_data.get("email") or getattr(inbox, "email", None),
                "recipient": settings.digest_recipient,
                "delivery_mode": settings.delivery_mode,
                "dry_run": settings.dry_run,
                "draft_test": draft_test,
            }
        )
    except Exception as exc:
        _print(
            {
                "ok": False,
                "stage": "api_call",
                "error": str(exc),
                "status_code": getattr(exc, "status_code", None),
            }
        )
        raise typer.Exit(code=1)


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
                sec_user_agent=settings.sec_user_agent,
                force=force,
                source_budget_seconds=settings.collect_source_budget_seconds,
                group_budget_seconds=settings.collect_group_budget_seconds,
                recovery_budget_seconds=settings.collect_recovery_budget_seconds,
                github_token=settings.github_token,
                openreview_access_token=settings.openreview_access_token,
                raw_store=raw_store,
            )
    finally:
        if raw_store is not None:
            raw_store.close()
    _print(stats.to_dict())
    if stats.budget_exhausted or (stats.sources > 0 and stats.failed == stats.sources):
        raise typer.Exit(code=1)


@app.command()
def enrich(
    limit: int = typer.Option(300, min=1, max=1000),
    summary_limit: int | None = typer.Option(None, min=0, max=100, help="LLM card cap"),
) -> None:
    settings, factory = _runtime()
    qwen = _model_client(settings)
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
            _print(
                {
                    **result,
                    "llm_provider": settings.llm_provider,
                    "llm_enabled": qwen.enabled,
                    "qwen_enabled": qwen.enabled,
                }
            )
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
            timeout_seconds=settings.agentmail_timeout_seconds,
            retry_budget_seconds=settings.agentmail_retry_budget_seconds,
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
            timeout_seconds=settings.agentmail_timeout_seconds,
            retry_budget_seconds=settings.agentmail_retry_budget_seconds,
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
    qwen = _model_client(settings)
    raw_store = _raw_store(settings)
    try:
        with session_scope(factory) as session:
            collection = {}
            for group in SourceGroup:
                collection[group.value] = collect_group(
                    session,
                    expanded,
                    group=group.value,
                    user_agent=settings.user_agent,
                    sec_user_agent=settings.sec_user_agent,
                    force=True,
                    source_budget_seconds=settings.collect_source_budget_seconds,
                    group_budget_seconds=settings.collect_group_budget_seconds,
                    recovery_budget_seconds=settings.collect_recovery_budget_seconds,
                    github_token=settings.github_token,
                    openreview_access_token=settings.openreview_access_token,
                    raw_store=raw_store,
                    cursor_transform=lambda source, cursor: _backfill_cursor_payload(
                        source, cursor, cutoff
                    ),
                    archive_only_cutoff=cutoff,
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


def _maintenance_runtime() -> tuple[Settings, object]:
    """Connect to an existing schema without issuer sync or schema creation."""
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        path = settings.database_url.removeprefix("sqlite:///")
        if path == ":memory:" or not Path(path).expanduser().is_file():
            raise typer.BadParameter("Maintenance requires an existing SQLite database")
    engine = create_db_engine(
        settings.database_url,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
        statement_timeout_seconds=settings.db_statement_timeout_seconds,
        lock_timeout_seconds=settings.db_lock_timeout_seconds,
    )
    if settings.app_env == "production" and engine.dialect.name != "postgresql":
        engine.dispose()
        raise RuntimeError("APP_ENV=production requires a PostgreSQL/Supabase database")
    return settings, session_factory(engine)


@app.command()
def maintenance(
    preview: bool = typer.Option(
        False, "--preview", help="Include private cleanup candidate paths; never delete."
    ),
    include_storage: bool = typer.Option(
        False, "--include-storage", help="Read bucket listings for the preview only."
    ),
) -> None:
    """Read-only capacity, source-health and retention diagnostics."""
    from .maintenance import diagnose, readonly_session

    if include_storage and not preview:
        raise typer.BadParameter("--include-storage requires --preview")
    settings, factory = _maintenance_runtime()
    raw_store = None
    storage_skip = "not_requested"
    try:
        if include_storage:
            if not settings.supabase_url or not settings.supabase_secret_key:
                storage_skip = "missing_storage_credentials"
            else:
                # Listing is an explicit read-only request, independent of the upload switch.
                raw_store = RawSnapshotStore(
                    supabase_url=settings.supabase_url,
                    secret_key=settings.supabase_secret_key,
                    bucket=settings.raw_storage_bucket,
                )
                storage_skip = None
        elif not settings.raw_storage_enabled:
            storage_skip = "raw_storage_disabled"
        with readonly_session(factory) as session:
            payload = diagnose(
                session, settings, preview=preview, raw_store=raw_store,
                storage_skip_reason=storage_skip,
            )
    finally:
        if raw_store is not None:
            raw_store.close()
        factory.kw["bind"].dispose()
    _print(payload)
    if payload["failure_reasons"]:
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
    qwen = _model_client(settings, chat_enabled=not rules_only)
    if not rules_only and not qwen.enabled:
        qwen.close()
        raise typer.BadParameter(
            "LLM_API_KEY is required; DASHSCOPE_API_KEY remains accepted for the "
            "DashScope provider. Use --rules-only only for offline diagnostics"
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
