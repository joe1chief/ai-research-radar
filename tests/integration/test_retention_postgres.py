"""Production-like vector/evidence schema on the isolated CI PostgreSQL service."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from ai_research_radar.db import (
    Base,
    ItemModel,
    ItemVersionModel,
    SourceModel,
    create_db_engine,
    session_factory,
)
from ai_research_radar.maintenance import readonly_session
from ai_research_radar.retention import estimate_retention, RetentionInspectionUnavailable


@pytest.fixture
def retention_pg():
    url = os.environ.get("RADAR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Use an isolated test database")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        available = connection.scalar(
            text("SELECT count(*) FROM pg_available_extensions WHERE name='vector'")
        )
        if not available:
            engine.dispose()
            if os.environ.get("RADAR_TEST_REQUIRE_VECTOR") == "true":
                pytest.fail("Required pgvector extension is unavailable")
            pytest.skip("pgvector CI image required for real vector/evidence schema")
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE public.evidence (item_version_id text REFERENCES public.item_versions(id))"
            )
        )
    factory = session_factory(engine)
    now = datetime.now(UTC)
    with factory.begin() as session:
        session.add(
            SourceModel(
                id="retention-source",
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
                id="retention-item",
                source_id="retention-source",
                canonical_url="https://example.invalid/one",
                item_type="article",
                title="Item",
                current_content_hash="current",
            )
        )
        session.flush()
        for name, days in [
            ("current", 150),
            ("previous", 100),
            ("candidate", 120),
            ("protected", 130),
        ]:
            session.add(
                ItemVersionModel(
                    id=name,
                    item_id="retention-item",
                    version_key=name,
                    content_hash=name,
                    title=name,
                    fetched_at=now - timedelta(days=days),
                    abstract_text="中",
                    normalized_text="正文",
                    embedding=[0.0] * 1024,
                    embedding_vector=[0.0] * 1024,
                )
            )
        session.flush()
        session.execute(text("INSERT INTO public.evidence VALUES ('protected')"))
    try:
        yield engine, factory, now
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS public.additional_reference"))
            connection.execute(text("DROP TABLE IF EXISTS public.evidence"))
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_real_vector_payload_estimate_preserves_evidence_and_is_readonly(retention_pg):
    engine, factory, now = retention_pg
    with readonly_session(factory) as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        result = estimate_retention(session, preview=True, now=now)
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["version_id"] == "candidate"
    assert result["candidate_logical_payload_bytes"] == 8192 + 9
    assert result["retention_reason_counts"]["evidence_reference"] == 1
    assert result["physical_reclaimable_bytes"] is None
    with factory() as session:
        assert session.scalar(text("SELECT count(*) FROM item_versions")) == 4
        assert (
            session.scalar(text("SELECT normalized_text FROM item_versions WHERE id='candidate'"))
            == "正文"
        )


def test_production_missing_evidence_fails_closed(retention_pg):
    engine, factory, now = retention_pg
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE public.evidence"))
    with readonly_session(factory) as session:
        with pytest.raises(RetentionInspectionUnavailable, match="evidence"):
            estimate_retention(session, now=now)


def test_additional_production_reference_fails_closed(retention_pg):
    engine, factory, now = retention_pg
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE public.additional_reference (version_id text REFERENCES item_versions(id))"
            )
        )
    with readonly_session(factory) as session:
        with pytest.raises(RetentionInspectionUnavailable, match="references"):
            estimate_retention(session, now=now)


def test_evidence_read_permission_failure_is_not_empty_inventory(retention_pg):
    engine, factory, now = retention_pg
    role = "retention_read_test"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        conn.execute(text(f"GRANT SELECT ON items,item_versions,event_items TO {role}"))
    try:
        with readonly_session(factory) as session:
            session.execute(text(f"SET LOCAL ROLE {role}"))
            with pytest.raises(Exception) as error:
                estimate_retention(session, now=now)
            assert getattr(error.value.orig, "sqlstate", None) == "42501"
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))
