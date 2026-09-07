"""Optional real PostgreSQL checks; use an isolated local test database only.

RADAR_TEST_POSTGRES_URL=postgresql+psycopg://... python -m pytest tests/integration
All fixtures live in temporary tables removed at transaction end.
"""
from __future__ import annotations

import json
import os
import random
import struct
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ai_research_radar.dedupe import cluster_decision, cosine_similarity
from ai_research_radar.pipeline import _cluster_candidate_query


@pytest.fixture
def pg_session():
    url = os.environ.get("RADAR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set RADAR_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
    engine = create_engine(url)
    with engine.connect() as conn, conn.begin():
        # Deliberately omit embedding_vector and do not install pgvector.
        for sql in (
            "CREATE TEMP TABLE events (id text PRIMARY KEY, cluster_id text, event_type text, "
            "first_seen_at timestamptz, entities text[], source_type text) ON COMMIT DROP",
            "CREATE TEMP TABLE item_versions (id text PRIMARY KEY, title text, embedding real[], "
            "metadata json, fetched_at timestamptz, abstract_text text, normalized_text text) ON COMMIT DROP",
            "CREATE TEMP TABLE event_items (event_id text, item_version_id text, relation text) ON COMMIT DROP",
        ):
            conn.execute(text(sql))
        with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
            yield session
            session.rollback()
    engine.dispose()


def add_candidates(session, vectors, now, *, text_size=0):
    events, versions, links = [], [], []
    for index, vector in enumerate(vectors):
        event_id = f"event-{index:04}"
        version_id = f"version-{index:04}"
        events.append(dict(id=event_id, now=now))
        versions.append(dict(id=version_id, now=now, vector=vector,
                             metadata=json.dumps({"embedding_space": "test-space"}),
                             body="x" * text_size))
        links.append(dict(event_id=event_id, version_id=version_id))
    session.execute(text("INSERT INTO events VALUES (:id, :id, 'PAPER', :now, ARRAY['lab'], 'rss')"), events)
    session.execute(text("INSERT INTO item_versions VALUES (:id, :id, :vector, CAST(:metadata AS json), :now, :body, :body)"), versions)
    session.execute(text("INSERT INTO event_items VALUES (:event_id, :version_id, 'primary')"), links)


def query(embedding, now):
    return _cluster_candidate_query(event_id="current", event_type="PAPER",
                                    threshold=now - timedelta(days=14), embedding=embedding,
                                    embedding_space="test-space", dialect_name="postgresql")


def test_postgres_top80_matches_python_with_1024_dimensions(pg_session):
    rng = random.Random(732)
    now = datetime.now(UTC)
    # Read stored REAL[] through psycopg, exactly as legacy ranking did.
    # Its text decoder uses printed decimals, not exact float32 promotion.
    f32 = lambda value: struct.unpack("f", struct.pack("f", value))[0]
    vectors = [[f32(rng.uniform(-1, 1)) for _ in range(1024)] for _ in range(1000)]
    embedding = [rng.uniform(-1, 1) for _ in range(1024)]
    add_candidates(pg_session, vectors, now, text_size=4096)
    legacy_started = time.perf_counter()
    legacy_rows = pg_session.execute(text(
        "SELECT e.*, v.title, v.abstract_text, v.normalized_text, v.embedding, v.metadata "
        "FROM events e JOIN event_items l ON l.event_id=e.id "
        "JOIN item_versions v ON v.id=l.item_version_id ORDER BY e.id"
    )).all()
    expected = sorted(
        [(row.id, cosine_similarity(embedding, row.embedding)) for row in legacy_rows],
        key=lambda row: (-row[1], row[0]),
    )[:80]
    legacy_elapsed = time.perf_counter() - legacy_started
    started = time.perf_counter()
    rows = pg_session.execute(query(embedding, now)).all()
    elapsed = time.perf_counter() - started
    assert [row.id for row in rows] == [row[0] for row in expected]
    for row, (_, score) in zip(rows, expected, strict=True):
        assert row.similarity == pytest.approx(score, abs=1e-12)
    assert set(rows[0]._mapping) == {"id", "cluster_id", "entities", "source_type", "version_id", "title", "similarity"}
    # Count encoded PostgreSQL result rows, excluding protocol framing, as a
    # reproducible payload proxy. This is not Supabase billing telemetry.
    old_rows = pg_session.execute(text(
        "SELECT octet_length(row_to_json(r)::text) FROM (SELECT e.*, v.title, "
        "v.abstract_text, v.normalized_text, v.embedding, v.metadata "
        "FROM events e JOIN event_items l ON l.event_id=e.id "
        "JOIN item_versions v ON v.id=l.item_version_id) r"
    )).scalars().all()
    new_bytes = sum(len(json.dumps(dict(row._mapping)).encode()) for row in rows)
    old_bytes = sum(old_rows)
    assert new_bytes < old_bytes / 100
    compiled = query(embedding, now).compile(
        dialect=pg_session.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )
    plan = pg_session.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + str(compiled))).scalar_one()[0]
    def subplans(node):
        found = [(node.get("Subplan Name"), node.get("Actual Loops"))] if "Subplan Name" in node else []
        return found + [value for child in node.get("Plans", []) for value in subplans(child)]
    print(f"\nScoring subplans: {subplans(plan['Plan'])}; JIT: {plan.get('JIT', {})}")
    print(f"\nPostgreSQL fixture: 1000 x 1024 -> {len(rows)} rows; "
          f"legacy fetch+Python rank={legacy_elapsed:.3f}s; database rank={elapsed:.3f}s; "
          f"encoded payload proxy={old_bytes} -> {new_bytes} bytes")


@pytest.mark.parametrize("embedding", [[1.0, 0.0], [0.0, 0.0], []])
def test_postgres_zero_mismatch_ties_and_thresholds(pg_session, embedding):
    now = datetime.now(UTC)
    vectors = [[0.0, 0.0], [1.0], [], [1.0, 0.0], [-1.0, 0.0],
               [0.84, (1 - 0.84**2)**0.5], [0.92, (1 - 0.92**2)**0.5],
               [0.84001, (1 - 0.84001**2)**0.5], [0.92001, (1 - 0.92001**2)**0.5]]
    add_candidates(pg_session, vectors, now)
    stored = pg_session.execute(text("SELECT id, embedding FROM item_versions ORDER BY id")).all()
    expected = sorted([(v.id.replace("version", "event"), cosine_similarity(embedding, v.embedding))
                       for v in stored if v.embedding], key=lambda row: (-row[1], row[0]))
    rows = pg_session.execute(query(embedding, now)).all()
    assert [row.id for row in rows] == [row[0] for row in expected]
    for row, (_, score) in zip(rows, expected, strict=True):
        assert row.similarity == pytest.approx(score, abs=1e-12)
        assert cluster_decision(row.similarity) == cluster_decision(score)


def test_postgres_latest_version_is_filtered_before_ranking(pg_session):
    now = datetime.now(UTC)
    add_candidates(pg_session, [[1.0, 0.0], [1.0, 0.0]], now)
    # A newer version in another space must suppress the older matching one.
    pg_session.execute(text("INSERT INTO item_versions VALUES ('newer', 'newer', ARRAY[1.0,0.0]::real[], "
                            "'{\"embedding_space\":\"different\"}', :now, '', '')"),
                       dict(now=now + timedelta(seconds=1)))
    pg_session.execute(text("INSERT INTO event_items VALUES ('event-0000', 'newer', 'primary')"))
    rows = pg_session.execute(query([1.0, 0.0], now)).all()
    assert [row.id for row in rows] == ['event-0001']
