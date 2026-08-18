from typer.testing import CliRunner

from ai_research_radar import cli
from ai_research_radar.contracts import SourceSpec
from ai_research_radar.db import (
    SourceCursorModel,
    SourceHealthModel,
    create_db_engine,
    init_schema,
    session_factory,
    sync_source,
)
from ai_research_radar.pipeline import CollectionStats
from ai_research_radar.settings import get_settings


def _runtime(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'radar.db'}")
    init_schema(engine)
    factory = session_factory(engine)
    settings = get_settings(
        RADAR_DATABASE_URL=f"sqlite:///{tmp_path / 'radar.db'}",
        RADAR_CONFIG_DIR="configs",
        RADAR_DRY_RUN=True,
        DELIVERY_MODE="shadow",
    )
    return settings, factory


def test_deliver_exits_nonzero_after_persisting_unknown_result(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda: runtime)
    monkeypatch.setattr(
        cli,
        "deliver_outbox",
        lambda *_args, **_kwargs: {
            "shadow": 0,
            "scheduled": 0,
            "sent": 0,
            "unknown": 1,
            "failed": 0,
        },
    )
    result = CliRunner().invoke(cli.app, ["deliver"])
    assert result.exit_code == 1
    assert '"unknown": 1' in result.stdout


def test_maintenance_exits_nonzero_for_three_consecutive_source_failures(
    tmp_path, monkeypatch
):
    settings, factory = _runtime(tmp_path)
    with factory.begin() as session:
        sync_source(
            session,
            SourceSpec(
                id="broken-source",
                entity_id="broken",
                group="tech",
                kind="rss",
                url="https://example.com/feed",
                fetch_strategy="rss",
                evidence_type="official_company",
                parser="rss",
            ),
        )
        session.add(
            SourceHealthModel(
                source_id="broken-source",
                status="failing",
                consecutive_failures=3,
                metadata_json={},
            )
        )
    monkeypatch.setattr(cli, "_runtime", lambda: (settings, factory))
    result = CliRunner().invoke(cli.app, ["maintenance"])
    assert result.exit_code == 1
    assert "broken-source" in result.stdout


def test_backfill_clears_http_validators_and_fails_on_degraded_source(
    tmp_path, monkeypatch
):
    settings, factory = _runtime(tmp_path)
    spec = SourceSpec(
        id="sec-backfill",
        entity_id="issuer",
        group="capital",
        kind="sec_submissions",
        url="https://data.sec.gov/submissions/CIK0000000001.json",
        fetch_strategy="sec_submissions",
        evidence_type="regulatory_filing",
        parser="sec_json",
    )
    with factory.begin() as session:
        sync_source(session, spec)
        session.add(
            SourceCursorModel(
                source_id=spec.id,
                cursor={"etag": "legacy", "last_modified": "legacy"},
                etag='"old"',
                last_modified="yesterday",
                last_seen_native_id="old-accession",
            )
        )

    def fake_collect(session, _sources, *, group, **_kwargs):
        cursor = session.get(SourceCursorModel, spec.id)
        assert cursor.etag is None
        assert cursor.last_modified is None
        assert cursor.last_seen_native_id is None
        return CollectionStats(degraded=1 if group == "capital" else 0)

    monkeypatch.setattr(cli, "_runtime", lambda: (settings, factory))
    monkeypatch.setattr(cli, "load_sources", lambda _config: [spec])
    monkeypatch.setattr(cli, "collect_group", fake_collect)
    monkeypatch.setattr(
        cli,
        "enrich_pending",
        lambda *_args, **_kwargs: {"processed": 0, "published": 0, "archived": 0},
    )
    result = CliRunner().invoke(cli.app, ["backfill", "--days", "14"])
    assert result.exit_code == 1
    assert '"degraded": 1' in result.stdout
