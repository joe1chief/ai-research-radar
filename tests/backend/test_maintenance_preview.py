import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from ai_research_radar import cli
from ai_research_radar.db import (
    ItemModel,
    ItemVersionModel,
    SourceModel,
    SourceHealthModel,
    UsageLedgerModel,
    create_db_engine,
    init_schema,
    session_factory,
)
from ai_research_radar.maintenance import readonly_session
from ai_research_radar.raw_storage import RawSnapshotStore
from ai_research_radar.settings import get_settings


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'radar.db'}"
    engine = create_db_engine(url)
    init_schema(engine)
    factory = session_factory(engine)
    settings = get_settings(
        RADAR_DATABASE_URL=url,
        RADAR_DRY_RUN=False,
        RADAR_RAW_STORAGE_ENABLED=True,
        SUPABASE_URL="https://storage.example.invalid",
        SUPABASE_SECRET_KEY="test-key",
    )
    with factory.begin() as session:
        session.add(
            SourceModel(
                id="test",
                entity_id="test",
                group="tech",
                kind="rss",
                url="https://example.invalid",
                fetch_strategy="rss",
                cadence="daily",
                evidence_type="official_company",
                parser="rss",
            )
        )
        session.flush()
        session.add(
            ItemModel(
                id="item",
                source_id="test",
                canonical_url="https://example.invalid",
                native_id="item",
                item_type="article",
                entity_id="test",
                title="Test",
                current_content_hash="a",
            )
        )
        session.flush()
        session.add(
            UsageLedgerModel(
                usage_date=date.today() - timedelta(days=70),
                usage_key="test",
                used=1,
                hard_limit=10,
            )
        )
        session.add_all(
            [
                ItemVersionModel(
                    id="old",
                    item_id="item",
                    version_key="old",
                    content_hash="a",
                    title="old",
                    fetched_at=datetime.now(UTC) - timedelta(days=20),
                    raw_storage_path="2020/01/01/shared.gz",
                ),
                ItemVersionModel(
                    id="new",
                    item_id="item",
                    version_key="new",
                    content_hash="b",
                    title="new",
                    fetched_at=datetime.now(UTC),
                    raw_storage_path="2020/01/01/shared.gz",
                ),
                ItemVersionModel(
                    id="old2",
                    item_id="item",
                    version_key="old2",
                    content_hash="c",
                    title="old2",
                    fetched_at=datetime.now(UTC) - timedelta(days=20),
                    raw_storage_path="2020/01/01/old.gz",
                ),
            ]
        )
    monkeypatch.setattr(cli, "_maintenance_runtime", lambda: (settings, factory))
    return settings, factory, engine


def test_preview_does_not_write_even_with_live_settings(runtime, monkeypatch):
    settings, factory, engine = runtime
    mutations = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}:
            mutations.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    monkeypatch.setattr(
        cli, "_runtime", lambda: pytest.fail("preview must not use mutating runtime")
    )
    monkeypatch.setattr(cli, "_raw_store", lambda _: pytest.fail("no implicit storage requests"))
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["read_only"] and payload["expired_raw_reference_count"] == 2
    assert payload["cleanup_preview"]["candidate_paths"] == ["2020/01/01/old.gz"]
    assert payload["cleanup_preview"]["protected_recent_reference_paths"] == [
        "2020/01/01/shared.gz"
    ]
    assert payload["storage_expired_object_count"] is None
    assert not mutations
    with factory() as session:
        assert session.get(ItemVersionModel, "old").raw_storage_path == "2020/01/01/shared.gz"
        assert session.scalar(select(UsageLedgerModel.usage_key)) == "test"


def test_default_summary_never_lists_or_deletes_storage(runtime, monkeypatch):
    monkeypatch.setattr(cli, "RawSnapshotStore", lambda **_: pytest.fail("no storage client"))
    result = CliRunner().invoke(cli.app, ["maintenance"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "cleanup_preview" not in payload
    assert payload["cleanup_skip_reason"] == "read_only_diagnostics"
    assert payload["expired_raw_objects_removed"] == 0


def test_explicit_storage_preview_only_lists_and_distinguishes_orphans(runtime, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and "/object/list/" in request.url.path
        prefix = json.loads(request.content)["prefix"]
        pages = {
            "": [{"name": "2020"}],
            "2020": [{"name": "01"}],
            "2020/01": [{"name": "01"}],
            "2020/01/01": [{"name": p, "id": p} for p in ["old.gz", "shared.gz", "orphan.gz"]],
        }
        return httpx.Response(200, json=pages.get(prefix, []))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = RawSnapshotStore(
        supabase_url="https://storage.example.invalid", secret_key="test-key", client=client
    )
    monkeypatch.setattr(cli, "RawSnapshotStore", lambda **_: store)
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview", "--include-storage"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["storage_expired_object_count"] == 3
    assert payload["storage_orphan_count"] == 1
    assert payload["cleanup_preview"]["confirmed_candidate_paths"] == [
        "2020/01/01/old.gz",
        "2020/01/01/orphan.gz",
    ]
    assert calls and payload["expired_raw_objects_removed"] == 0
    client.close()


def test_sqlite_readonly_guard_blocks_write_and_resets(runtime):
    _, factory, _ = runtime
    with pytest.raises(DBAPIError):
        with readonly_session(factory) as session:
            session.execute(text("DELETE FROM item_versions"))
    with factory.begin() as session:
        assert session.scalar(select(ItemVersionModel.id).limit(1))
        session.execute(text("UPDATE item_versions SET title=title"))


def test_production_pending_references_remain_failure(runtime, monkeypatch):
    settings, factory, _ = runtime
    monkeypatch.setattr(
        cli,
        "_maintenance_runtime",
        lambda: (settings.model_copy(update={"app_env": "production"}), factory),
    )
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview"])
    assert result.exit_code == 1
    assert "expired_raw_references_pending" in result.stdout


def test_storage_requires_explicit_preview():
    result = CliRunner().invoke(cli.app, ["maintenance", "--include-storage"])
    assert result.exit_code == 2


def test_real_readonly_runtime_never_initializes_schema_or_syncs(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    url = f"sqlite:///{path}"
    monkeypatch.setattr(cli, "get_settings", lambda: get_settings(RADAR_DATABASE_URL=url))
    monkeypatch.setattr(cli, "init_schema", lambda _: pytest.fail("no DDL"))
    monkeypatch.setattr(cli, "sync_issuers", lambda *_: pytest.fail("no issuer sync"))
    settings, factory = cli._maintenance_runtime()
    with readonly_session(factory) as session:
        assert session.scalar(text("SELECT count(*) FROM sqlite_master WHERE type='table'")) == 0
    factory.kw["bind"].dispose()


def test_all_health_reasons_retained_and_legacy_errors_redacted(runtime, monkeypatch):
    from ai_research_radar import maintenance

    settings, factory, _ = runtime
    with factory.begin() as session:
        session.add(
            SourceHealthModel(
                source_id="test",
                status="failing",
                consecutive_failures=3,
                last_error="source failed: error_type=RuntimeError secret=private",
                metadata_json={"secret": "private"},
            )
        )
    assert maintenance.CAPACITY_WARNING_BYTES == 350 * 1024 * 1024
    monkeypatch.setattr(maintenance, "CAPACITY_WARNING_BYTES", 1)
    monkeypatch.setattr(
        cli,
        "_maintenance_runtime",
        lambda: (settings.model_copy(update={"app_env": "production"}), factory),
    )
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["failure_reasons"] == [
        "database_capacity_warning",
        "persistent_source_failures",
        "expired_raw_references_pending",
    ]
    assert payload["source_failure_details"][0]["error_type"] == "RuntimeError"
    assert "private" not in result.stdout and "last_error" not in result.stdout


def test_storage_listing_failure_is_safe_and_does_not_clear_pending(runtime, monkeypatch):
    class BrokenStore:
        def list_older_than(self, cutoff):
            raise RuntimeError("private URL and credentials")

        def close(self):
            pass

    monkeypatch.setattr(cli, "RawSnapshotStore", lambda **_: BrokenStore())
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview", "--include-storage"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["expired_raw_reference_count"] == 2
    assert payload["storage_listing_skip_reason"] == "storage_listing_failed"
    assert not payload["storage_listing_performed"]
    assert payload["storage_expired_object_count"] is None
    assert "private URL" not in result.stdout


def test_missing_storage_credentials_is_explicit_failure(runtime, monkeypatch):
    settings, factory, _ = runtime
    settings = settings.model_copy(update={"supabase_url": None, "supabase_secret_key": None})
    monkeypatch.setattr(cli, "_maintenance_runtime", lambda: (settings, factory))
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview", "--include-storage"])
    assert result.exit_code == 1
    assert "missing_storage_credentials" in result.stdout


def test_storage_inventory_depth_limit_fails_closed():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("no request beyond depth limit"))
    ) as client:
        store = RawSnapshotStore(
            supabase_url="https://example.invalid", secret_key="test-key", client=client
        )
        with pytest.raises(RuntimeError, match="incomplete"):
            store._walk_objects("prefix", depth=9)


def test_preview_does_not_create_missing_sqlite_database(tmp_path, monkeypatch):
    path = tmp_path / "new-directory" / "missing.db"
    monkeypatch.setattr(
        cli, "get_settings", lambda: get_settings(RADAR_DATABASE_URL=f"sqlite:///{path}")
    )
    result = CliRunner().invoke(cli.app, ["maintenance", "--preview"])
    assert result.exit_code == 2
    assert not path.parent.exists()


def test_storage_listing_page_limit_fails_closed():
    page = [{"name": str(i)} for i in range(1000)]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page))
    ) as client:
        store = RawSnapshotStore(
            supabase_url="https://example.invalid", secret_key="test-key", client=client
        )
        with pytest.raises(RuntimeError, match="incomplete"):
            store._list_prefix("")
