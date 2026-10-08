"""Run only with RADAR_TEST_POSTGRES_URL pointing to an isolated test database."""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from ai_research_radar.contracts import CollectionBatch, CollectedItem, SourceSpec
from ai_research_radar.db import (
    create_db_engine,
    SourceModel,
    SourceCursorModel,
    SourceHealthModel,
    sync_source,
)
from ai_research_radar.limits import Deadline
from ai_research_radar.pipeline import collect_group


@pytest.fixture
def pg_engine():
    url = os.environ.get("RADAR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set RADAR_TEST_POSTGRES_URL to an isolated PostgreSQL database")
    engine = create_db_engine(url, statement_timeout_seconds=0.25, lock_timeout_seconds=0.05)
    try:
        yield engine
    finally:
        engine.dispose()


def test_statement_timeout_and_transaction_recovery(pg_engine):
    with Session(pg_engine) as session:
        assert session.scalar(text("SHOW statement_timeout")) == "250ms"
        assert session.scalar(text("SHOW lock_timeout")) == "50ms"
        with pytest.raises(DBAPIError) as error:
            session.execute(text("SELECT pg_sleep(2)"))
        assert error.value.orig.sqlstate == "57014"
        session.rollback()
        assert session.scalar(text("SELECT 1")) == 1
        assert session.scalar(text("SHOW statement_timeout")) == "250ms"


def test_lock_timeout_then_healthy_transaction(pg_engine):
    key = uuid.uuid4().int % (2**62)
    with pg_engine.connect() as owner, Session(pg_engine) as session:
        owner.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        with pytest.raises(DBAPIError) as error:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        assert error.value.orig.sqlstate == "55P03"
        session.rollback()
        owner.rollback()
        assert session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}) is True


def test_source_remaining_budget_caps_statement_timeout(pg_engine):
    with Session(pg_engine) as session:
        session.scalar(text("SELECT 1"))  # finish connection initialization first
        with Deadline(0.1).activate():
            timeout = session.scalar(text("SHOW statement_timeout"))
            assert int(timeout.removesuffix("ms")) <= 100
            with pytest.raises(DBAPIError) as error:
                session.execute(text("SELECT pg_sleep(2)"))
        assert error.value.orig.sqlstate == "57014"
        session.rollback()
        assert session.scalar(text("SELECT 1")) == 1


def test_real_timeout_preserves_cursor_and_collection_continues(pg_engine, monkeypatch):
    bad = SourceSpec(
        id="slow",
        entity_id="test",
        group="tech",
        kind="rss",
        url="https://example.invalid",
        fetch_strategy="rss",
        parser="rss",
        evidence_type="official_company",
        allow_empty=True,
    )
    good = bad.model_copy(update={"id": "healthy"})

    class Collector:
        def __init__(self, spec):
            self.spec = spec

        def collect(self, cursor):
            items = (
                [
                    CollectedItem(
                        source_id=bad.id,
                        external_id="one",
                        canonical_url="https://example.invalid/one",
                        title="Agent",
                    )
                ]
                if self.spec.id == bad.id
                else []
            )
            return CollectionBatch(items=items, cursor={"position": "new"})

        def close(self):
            pass

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for", lambda spec, **kwargs: Collector(spec)
    )
    monkeypatch.setattr(
        "ai_research_radar.pipeline.ingest_item",
        lambda session, *_: session.execute(text("SELECT pg_sleep(2)")),
    )
    with pg_engine.connect() as connection:
        # Session-bound connection keeps these temporary tables across source
        # commits. No persistent schema or production tables are touched.
        for model in (SourceModel, SourceCursorModel, SourceHealthModel):
            ddl = str(CreateTable(model.__table__).compile(dialect=pg_engine.dialect))
            connection.exec_driver_sql(ddl.replace("CREATE TABLE", "CREATE TEMP TABLE", 1))
        connection.commit()
        with Session(bind=connection, expire_on_commit=False) as session:
            sync_source(session, bad)
            session.add(SourceCursorModel(source_id=bad.id, cursor={"position": "old"}))
            session.commit()
            stats = collect_group(session, [bad, good], group="tech", user_agent="test")
            assert stats.failed == 1 and stats.sources == 2
            assert session.get(SourceCursorModel, bad.id).cursor == {"position": "old"}
            assert session.get(SourceHealthModel, bad.id).status == "failing"
            assert session.get(SourceHealthModel, good.id).status == "healthy"
            assert session.get(SourceCursorModel, good.id).cursor == {"position": "new"}
