"""Behavioral regressions for bounded, database-ranked cluster retrieval."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import sqrt
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from ai_research_radar.contracts import SourceSpec
from ai_research_radar.db import (
    EventItemModel,
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    sync_source,
)
from ai_research_radar.pipeline import _choose_cluster, _cluster_candidate_query


@pytest.fixture
def candidates(session):
    now = datetime.now(UTC)
    sync_source(session, SourceSpec(
        id="egress-test", entity_id="lab", group="tech", kind="rss",
        url="https://example.com/feed", fetch_strategy="rss",
        evidence_type="official_company", parser="rss",
    ))

    def add(identifier, vector, *, space="test-space", event_type="MODEL_RELEASE",
            title=None, entities=None, version="v1", fetched_at=None,
            abstract="selected abstract", body="selected body"):
        item_id = f"item-{identifier}"
        if session.get(ItemModel, item_id) is None:
            session.add(ItemModel(
                id=item_id, source_id="egress-test", native_id=identifier,
                canonical_url=f"https://example.com/{identifier}", item_type="rss",
                entity_id="lab", title=title or identifier,
                current_content_hash=sha256(identifier.encode()).hexdigest(),
            ))
            session.add(RadarEventModel(
                id=identifier, cluster_id=identifier, event_type=event_type,
                entities=["lab"] if entities is None else entities,
                title_zh=identifier, summary_zh="summary", why_it_matters="why",
                status="NEW_ENTITY", source_type="rss",
                verification_status="company_claim", score=80,
                primary_url=f"https://example.com/{identifier}", first_seen_at=now,
            ))
            session.flush()
        version_id = f"{identifier}-{version}"
        session.add(ItemVersionModel(
            id=version_id, item_id=item_id, version_key=version,
            content_hash=sha256(version_id.encode()).hexdigest(),
            title=title or identifier, embedding=vector,
            metadata_json={"embedding_space": space}, fetched_at=fetched_at or now,
            abstract_text=abstract, normalized_text=body,
        ))
        session.flush()
        session.add(EventItemModel(event_id=identifier, item_version_id=version_id,
                                   relation="primary"))
        session.flush()
        return version_id

    return add


def ranked(session, embedding=None, event_type="MODEL_RELEASE"):
    return session.execute(_cluster_candidate_query(
        event_id="incoming", event_type=event_type,
        threshold=datetime.now(UTC) - timedelta(days=14),
        embedding=[1.0, 0.0] if embedding is None else embedding,
        embedding_space="test-space", dialect_name="sqlite",
    )).all()


def choose(session, *, event_type="MODEL_RELEASE", title="incoming", qwen=None):
    return _choose_cluster(
        session, item=ItemModel(title=title, entity_id="lab", item_type="rss"),
        event_id="incoming", event_type=event_type, embedding=[1.0, 0.0],
        embedding_space="test-space", qwen=qwen, item_text="incoming text",
    )


def test_top_80_ranks_before_limit_and_breaks_ties_by_event_id(session, candidates):
    # Insert tied rows in reverse order: neither insertion order nor an early
    # ID-based LIMIT may displace the best match at the end of the alphabet.
    for index in reversed(range(85)):
        candidates(f"a-{index:03}", [0.0, 1.0])
    candidates("z-best", [1.0, 0.0])
    rows = ranked(session)
    assert [row.id for row in rows] == ["z-best", *[f"a-{i:03}" for i in range(79)]]
    assert rows[0].similarity == pytest.approx(1.0)
    assert all(row.similarity == 0.0 for row in rows[1:])
    # The result crossing the database boundary must contain neither vectors
    # nor full text, even when those values are present in the stored version.
    assert set(rows[0]._mapping) == {
        "id", "cluster_id", "entities", "source_type", "version_id", "title", "similarity",
    }


@pytest.mark.parametrize("latest_vector,latest_space", [
    ([1.0, 0.0], "different-space"), ([], "test-space"), (None, "test-space"),
])
def test_latest_ineligible_primary_does_not_fall_back_to_old_version(
    session, candidates, latest_vector, latest_space,
):
    now = datetime.now(UTC)
    candidates("candidate", [1.0, 0.0], fetched_at=now - timedelta(hours=1))
    candidates("candidate", latest_vector, space=latest_space, version="v2", fetched_at=now)
    assert ranked(session) == []


@pytest.mark.parametrize("query", [[1.0, 0.0], [0.0, 0.0], []])
def test_empty_candidates_excluded_but_zero_and_wrong_dimensions_rank_as_zero(
    session, candidates, query,
):
    candidates("a-zero", [0.0, 0.0])
    candidates("b-short", [1.0])
    candidates("c-long", [1.0, 0.0, 0.0])
    candidates("d-empty", [])
    candidates("e-null", None)
    assert [(row.id, row.similarity) for row in ranked(session, query)] == [
        ("a-zero", 0.0), ("b-short", 0.0), ("c-long", 0.0),
    ]


@pytest.mark.parametrize("event_type", ["MODEL_RELEASE", "PAPER"])
def test_entity_filter_and_same_title_promotion_happen_after_top_80(
    session, candidates, event_type,
):
    top_similarity = 0.99 if event_type == "MODEL_RELEASE" else 0.5
    excluded_similarity = 0.95 if event_type == "MODEL_RELEASE" else 0.0
    for index in range(80):
        candidates(f"a-{index:03}", [top_similarity, sqrt(1 - top_similarity ** 2)], event_type=event_type,
                   entities=["other"], title="unrelated")
    candidates("z-title-match", [excluded_similarity, sqrt(1 - excluded_similarity ** 2)],
               event_type=event_type, title="incoming")
    assert choose(session, event_type=event_type) == ("incoming", False, None)


def test_paper_same_title_zero_vector_inside_budget_still_merges(session, candidates):
    candidates("same-title", [0.0, 0.0], event_type="PAPER", title="Incoming")
    assert choose(session, event_type="PAPER") == ("same-title", True, "same-title")


@pytest.mark.parametrize("similarity,with_qwen,expected_reads,merged", [
    (1.0, True, 0, True), (0.9, True, 1, True),
    (0.9, False, 0, False), (0.5, True, 0, False),
])
def test_full_text_is_read_only_for_model_review(
    session, candidates, similarity, with_qwen, expected_reads, merged,
):
    candidates("candidate", [similarity, sqrt(1.0 - similarity ** 2)])
    texts = []
    def adjudicate(left, right):
        texts.append((left, right))
        return SimpleNamespace(same_event=True)
    qwen = SimpleNamespace(adjudicate_merge=adjudicate) if with_qwen else None
    reads = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "abstract_text" in statement:
            reads.append(statement)
    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = choose(session, qwen=qwen)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result == (("candidate", True, "candidate") if merged else ("incoming", False, None))
    assert len(reads) == expected_reads
    assert texts == ([('incoming text', 'candidate\nselected abstract\nselected body')]
                     if expected_reads else [])


def test_review_fetches_ranked_version_when_new_primary_arrives(session, candidates, monkeypatch):
    candidates("candidate", [0.9, sqrt(0.19)])
    execute = session.execute
    advanced = False
    def advance_after_ranking(statement, *args, **kwargs):
        nonlocal advanced
        result = execute(statement, *args, **kwargs)
        if not advanced and "eligible_cluster_events" in str(statement):
            advanced = True
            frozen = result.freeze()
            candidates("candidate", [1.0, 0.0], version="v2",
                       fetched_at=datetime.now(UTC) + timedelta(seconds=1),
                       title="new title", abstract="new abstract", body="new body")
            return frozen()
        return result
    monkeypatch.setattr(session, "execute", advance_after_ranking)
    texts = []
    def adjudicate(left, right):
        texts.append(right)
        return SimpleNamespace(same_event=True)
    assert choose(session, qwen=SimpleNamespace(adjudicate_merge=adjudicate)) == (
        "candidate", True, "candidate",
    )
    assert advanced
    assert texts == ["candidate\nselected abstract\nselected body"]
