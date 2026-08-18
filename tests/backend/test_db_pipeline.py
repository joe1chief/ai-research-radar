from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text as sql_text
from sqlalchemy.dialects import postgresql

from ai_research_radar.contracts import CollectionBatch, CollectedItem, EventStatus, SourceSpec
from ai_research_radar.db import (
    EventItemModel,
    EventRevisionModel,
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    SourceHealthModel,
    UsageLedgerModel,
    ingest_item,
    reserve_daily_usage,
    sync_source,
)
from ai_research_radar.llm import EmbeddingResult, QwenResult, deterministic_embedding
from ai_research_radar.pipeline import (
    _cluster_candidate_query,
    _combined_verification,
    collect_group,
    editorialize_top,
    enrich_pending,
    recover_pending_embeddings,
)
from ai_research_radar.topics import RuleTopicClassifier


ROOT = Path(__file__).parents[2]


def test_postgres_cluster_candidate_sql_uses_table_owned_float_array():
    statement = _cluster_candidate_query(
        event_id="event-current",
        event_type="PAPER",
        threshold=datetime(2026, 8, 1, tzinfo=UTC),
    )

    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "item_versions.embedding" in compiled
    assert "embedding_vector" not in compiled
    assert "<=>" not in compiled
    assert "eligible_cluster_events" in compiled
    assert "events.first_seen_at" in compiled


def test_cluster_candidate_query_bounds_history_and_uses_latest_primary(session):
    now = datetime.now(UTC)
    spec = source("cluster-query", entity="openai")
    sync_source(session, spec)
    item = ItemModel(
        id="cluster-item",
        source_id=spec.id,
        native_id="cluster-native",
        canonical_url="https://example.com/cluster-query/item",
        item_type="rss",
        entity_id="openai",
        title="Candidate item",
        current_content_hash="a" * 64,
        metadata_json={},
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    event = RadarEventModel(
        id="cluster-event",
        cluster_id="cluster-event",
        event_type="MODEL_RELEASE",
        topics=["agent_systems"],
        entities=["openai"],
        cross_tags=[],
        title_zh="Candidate event",
        summary_zh="summary",
        why_it_matters="why",
        status="NEW_ENTITY",
        source_type="rss",
        verification_status="company_claim",
        score=80,
        primary_url="https://example.com/cluster-query/item",
        corroborating_urls=[],
        is_public=True,
        first_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add_all([item, event])
    session.flush()
    for version_id, title, fetched_at in (
        ("cluster-version-old", "Older primary", now - timedelta(hours=1)),
        ("cluster-version-new", "Latest primary", now),
    ):
        session.add(
            ItemVersionModel(
                id=version_id,
                item_id=item.id,
                version_key=version_id,
                content_hash=("b" if version_id.endswith("old") else "c") * 64,
                title=title,
                fetched_at=fetched_at,
                embedding=[1.0, *([0.0] * 1023)],
                metadata_json={"embedding_space": "test-space"},
            )
        )
        session.flush()
        session.add(
            EventItemModel(
                event_id=event.id,
                item_version_id=version_id,
                relation="primary",
            )
        )
    session.flush()

    rows = session.execute(
        _cluster_candidate_query(
            event_id="different-event",
            event_type=event.event_type,
            threshold=now - timedelta(days=14),
        )
    ).all()
    assert len(rows) == 1
    assert rows[0]._mapping["title"] == "Latest primary"

    expired = session.execute(
        _cluster_candidate_query(
            event_id="different-event",
            event_type=event.event_type,
            threshold=now + timedelta(seconds=1),
        )
    ).all()
    assert expired == []


def source(source_id="lab", *, entity="lab", kind="rss", evidence="official_company"):
    return SourceSpec(
        id=source_id,
        entity_id=entity,
        group="tech",
        kind=kind,
        url=f"https://example.com/{source_id}",
        fetch_strategy=kind,
        evidence_type=evidence,
        parser="rss",
    )


def test_ingest_is_idempotent_and_arxiv_version_is_material(session):
    spec = source("arxiv", entity="arxiv", kind="arxiv_api", evidence="paper")
    sync_source(session, spec)
    v1 = CollectedItem(
        source_id=spec.id,
        external_id="2501.12345",
        canonical_url="https://arxiv.org/abs/2501.12345v1",
        title="Long-Horizon Agent",
        summary="A long-term planning agent",
        metadata={"version": 1},
    )
    row, changed = ingest_item(session, spec, v1)
    assert changed
    _, changed_again = ingest_item(session, spec, v1)
    assert not changed_again
    v2 = v1.model_copy(
        update={
            "canonical_url": "https://arxiv.org/abs/2501.12345v2",
            "summary": "A long-term planning agent with new experiments",
            "metadata": {"version": 2},
        }
    )
    row, changed_v2 = ingest_item(session, spec, v2)
    assert changed_v2
    assert row.metadata_json["update_status"] == EventStatus.MATERIAL_UPDATE.value
    assert "版本 1→2" in row.metadata_json["change_summary"]
    assert "核心摘要" in row.metadata_json["change_summary"]
    assert session.scalar(select(func.count()).select_from(ItemVersionModel)) == 2


def test_sql_pending_filter_cannot_starve_an_older_unprocessed_item(session):
    spec = source("backlog")
    sync_source(session, spec)
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    pending = None
    for index in range(5):
        row, _ = ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=str(index),
                canonical_url=f"https://example.com/backlog/{index}",
                title=f"Autonomous agent memory release {index}",
                summary="autonomous agent runtime and persistent memory",
            ),
        )
        row.first_seen_at = datetime.now(UTC) - timedelta(minutes=index)
        if index < 4:
            row.metadata_json = {
                **(row.metadata_json or {}),
                "processed_hash": row.current_content_hash,
            }
        else:
            pending = row
    result = enrich_pending(
        session,
        classifier=classifier,
        config_dir=ROOT / "configs",
        limit=1,
    )
    assert result["processed"] == 1
    assert pending is not None
    assert pending.metadata_json["processed_hash"] == pending.current_content_hash


def test_backfill_cutoff_archives_old_or_undated_records_without_events(session):
    spec = source("backfill-cutoff")
    sync_source(session, spec)
    cutoff = datetime.now(UTC) - timedelta(days=14)
    for native_id, published_at in (
        ("old", cutoff - timedelta(days=1)),
        ("undated", None),
    ):
        ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=native_id,
                canonical_url=f"https://example.com/backfill-cutoff/{native_id}",
                title="Autonomous agent memory release",
                summary="autonomous agent runtime and persistent memory",
                published_at=published_at,
            ),
        )
    result = enrich_pending(
        session,
        classifier=RuleTopicClassifier.from_config(ROOT / "configs"),
        config_dir=ROOT / "configs",
        limit=10,
        suppress_delivery=True,
        source_time_cutoff=cutoff,
    )
    assert result["archived"] == 2
    assert session.scalar(select(func.count()).select_from(RadarEventModel)) == 0
    assert all(
        item.metadata_json["backfill_outside_window"]
        for item in session.scalars(select(ItemModel)).all()
    )


class _EmbeddingQwen:
    enabled = True

    def __init__(self, space: str):
        self.space = space

    def enhance(self, *_args, **_kwargs):
        return None

    def embed_with_provenance(self, text: str):
        if self.space == "feature-hash-v1":
            return EmbeddingResult(deterministic_embedding(text), self.space)
        return EmbeddingResult([1.0, *([0.0] * 1023)], self.space)

    def adjudicate_merge(self, *_args, **_kwargs):
        return None


def test_embedding_outage_cards_are_withheld_then_reembedded_and_reclustered(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    first_source = source("embedding-one", entity="openai")
    second_source = source("embedding-two", entity="openai")
    for spec in (first_source, second_source):
        sync_source(session, spec)

    ingest_item(
        session,
        first_source,
        CollectedItem(
            source_id=first_source.id,
            external_id="one",
            canonical_url="https://example.com/embedding-one/event",
            title="OpenAI autonomous agent persistent memory release",
            summary="New autonomous agent runtime with persistent memory",
            entity_id="openai",
            evidence_type="official_company",
        ),
    )
    first = enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("feature-hash-v1"),
        config_dir=ROOT / "configs",
    )
    assert first["embedding_pending"] == 1
    first_event = session.scalar(select(RadarEventModel))
    assert first_event is not None
    assert first_event.is_public is False
    assert first_event.delivery_suppressed is True

    ingest_item(
        session,
        second_source,
        CollectedItem(
            source_id=second_source.id,
            external_id="two",
            canonical_url="https://example.com/embedding-two/event",
            title="OpenAI autonomous agent persistent memory release",
            summary="New autonomous agent runtime with persistent memory",
            entity_id="openai",
            evidence_type="official_company",
        ),
    )
    enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        config_dir=ROOT / "configs",
    )
    assert len(session.scalars(select(RadarEventModel)).all()) == 2

    recovered = recover_pending_embeddings(
        session,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        limit=10,
        daily_limit=10,
    )
    assert recovered["reembedded"] == 1
    assert recovered["merged"] == 1
    events = session.scalars(select(RadarEventModel)).all()
    assert sum(event.cluster_id == event.id for event in events) == 1
    assert sum(event.is_public for event in events) == 1


def test_embedding_recovery_does_not_unsuppress_archive_only_backfill(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    spec = source("embedding-backfill", entity="openai")
    sync_source(session, spec)
    ingest_item(
        session,
        spec,
        CollectedItem(
            source_id=spec.id,
            external_id="backfill",
            canonical_url="https://example.com/embedding-backfill/event",
            title="OpenAI autonomous agent persistent memory release",
            summary="New autonomous agent runtime with persistent memory",
            entity_id="openai",
            evidence_type="official_company",
        ),
    )
    enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("feature-hash-v1"),
        config_dir=ROOT / "configs",
        suppress_delivery=True,
    )
    recovered = recover_pending_embeddings(
        session,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        limit=10,
        daily_limit=10,
    )
    assert recovered["reembedded"] == 1
    model = session.scalar(select(RadarEventModel))
    assert model is not None
    assert model.is_public is True
    assert model.delivery_suppressed is True


def test_embedding_recovery_preserves_preexisting_non_backfill_suppression(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    spec = source("embedding-pre-suppressed", entity="openai")
    sync_source(session, spec)
    original = CollectedItem(
        source_id=spec.id,
        external_id="pre-suppressed",
        canonical_url="https://example.com/embedding-pre-suppressed/event",
        title="OpenAI autonomous agent persistent memory release",
        summary="New autonomous agent runtime with persistent memory",
        entity_id="openai",
        evidence_type="official_company",
    )
    ingest_item(session, spec, original)
    enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        config_dir=ROOT / "configs",
    )
    model = session.scalar(select(RadarEventModel))
    assert model is not None
    model.delivery_suppressed = True

    ingest_item(
        session,
        spec,
        original.model_copy(
            update={
                "summary": (
                    "New autonomous agent runtime with persistent memory and a revised safety claim"
                )
            }
        ),
    )
    enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("feature-hash-v1"),
        config_dir=ROOT / "configs",
    )
    recover_pending_embeddings(
        session,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        limit=10,
        daily_limit=10,
    )
    assert model.delivery_suppressed is True


def test_material_update_during_embedding_outage_releases_old_backfill_suppression(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    spec = source("embedding-backfill-update", entity="openai")
    sync_source(session, spec)
    original = CollectedItem(
        source_id=spec.id,
        external_id="backfill-update",
        canonical_url="https://example.com/embedding-backfill-update/event",
        title="OpenAI autonomous agent persistent memory release",
        summary="New autonomous agent runtime with persistent memory",
        entity_id="openai",
        evidence_type="official_company",
    )
    ingest_item(session, spec, original)
    enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        config_dir=ROOT / "configs",
        suppress_delivery=True,
    )
    model = session.scalar(select(RadarEventModel))
    assert model is not None
    assert model.delivery_suppressed is True

    ingest_item(
        session,
        spec,
        original.model_copy(
            update={
                "title": "OpenAI autonomous agent persistent memory v2 release",
                "summary": "New experiments materially revise the autonomous agent memory result",
            }
        ),
    )
    enrich_pending(
        session,
        classifier=classifier,
        qwen=_EmbeddingQwen("feature-hash-v1"),
        config_dir=ROOT / "configs",
        suppress_delivery=False,
    )
    assert model.status == "MATERIAL_UPDATE"
    assert model.delivery_suppressed is True

    recover_pending_embeddings(
        session,
        qwen=_EmbeddingQwen("text-embedding-v4"),
        limit=10,
        daily_limit=10,
    )
    assert model.delivery_suppressed is False
    assert model.is_public is True


def test_rotated_feed_guid_reuses_the_same_canonical_item(session):
    spec = source("rotated-guid")
    sync_source(session, spec)
    first = CollectedItem(
        source_id=spec.id,
        external_id="guid-old",
        canonical_url="https://example.com/rotated-guid/post",
        title="Agent memory post",
        summary="autonomous agent memory",
    )
    row, changed = ingest_item(session, spec, first)
    assert changed
    same, changed = ingest_item(
        session,
        spec,
        first.model_copy(update={"external_id": "guid-new"}),
    )
    assert same.id == row.id
    assert not changed
    assert session.scalar(select(func.count()).select_from(ItemModel)) == 1


def test_daily_usage_reservation_persists_and_enforces_hard_limit(session):
    usage_date = date(2026, 7, 12)
    assert reserve_daily_usage(
        session,
        usage_date=usage_date,
        usage_key="qwen_flash",
        hard_limit=2,
    )
    assert reserve_daily_usage(
        session,
        usage_date=usage_date,
        usage_key="qwen_flash",
        hard_limit=2,
    )
    assert not reserve_daily_usage(
        session,
        usage_date=usage_date,
        usage_key="qwen_flash",
        hard_limit=2,
    )
    row = session.get(UsageLedgerModel, (usage_date, "qwen_flash"))
    assert row is not None
    assert row.used == 2


def test_collection_uploads_ephemeral_raw_html_and_stores_only_private_path(
    session, monkeypatch
):
    spec = source("raw-html", entity="openai", kind="html")
    item = CollectedItem(
        source_id=spec.id,
        external_id="post-1",
        canonical_url="https://example.com/raw-html/post-1",
        title="Autonomous Agent Runtime",
        summary="multi-agent memory runtime",
        content="multi-agent memory runtime release",
        raw_snapshot=b"<html><article>private response</article></html>",
    )

    class Collector:
        def collect(self, cursor):
            return CollectionBatch(items=[item], cursor=cursor)

        def close(self):
            return None

    class Store:
        def __init__(self):
            self.payload = None

        def put(self, **kwargs):
            self.payload = kwargs["payload"]
            return "2026/07/12/raw-html/item/content.html.gz"

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for",
        lambda *args, **kwargs: Collector(),
    )
    store = Store()
    stats = collect_group(
        session,
        [spec],
        group="tech",
        user_agent="AIResearchRadar/test",
        raw_store=store,
    )
    version = session.scalar(select(ItemVersionModel))
    assert stats.changed == 1
    assert store.payload == item.raw_snapshot
    assert version.raw_storage_path.endswith("content.html.gz")
    assert "private response" not in json.dumps(version.metadata_json)


def test_source_savepoint_keeps_healthy_results_after_database_failure(session, monkeypatch):
    good = source("good-source")
    bad = source("bad-source")
    original_ingest = ingest_item

    class Collector:
        def __init__(self, spec):
            self.spec = spec

        def collect(self, cursor):
            return CollectionBatch(
                items=[
                    CollectedItem(
                        source_id=self.spec.id,
                        external_id="one",
                        canonical_url=f"https://example.com/{self.spec.id}/one",
                        title="Autonomous agent memory",
                        summary="multi-agent memory runtime",
                    )
                ],
                cursor=cursor,
            )

        def close(self):
            return None

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for",
        lambda spec, **kwargs: Collector(spec),
    )

    def sometimes_fails(active_session, spec, item):
        if spec.id == bad.id:
            active_session.execute(sql_text("insert into items (id) values ('broken')"))
        return original_ingest(active_session, spec, item)

    monkeypatch.setattr("ai_research_radar.pipeline.ingest_item", sometimes_fails)
    stats = collect_group(
        session,
        [good, bad],
        group="tech",
        user_agent="AIResearchRadar/test",
    )
    assert stats.failed == 1
    assert session.scalar(select(func.count()).select_from(ItemModel)) == 1


def test_empty_usable_result_marks_source_degraded_instead_of_green(session, monkeypatch):
    spec = source("empty-source")

    class EmptyCollector:
        def collect(self, cursor):
            return CollectionBatch(cursor=cursor)

        def close(self):
            return None

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for",
        lambda *args, **kwargs: EmptyCollector(),
    )
    collect_group(
        session,
        [spec],
        group="tech",
        user_agent="AIResearchRadar/test",
    )
    health = session.get(SourceHealthModel, spec.id)
    assert health.status == "degraded"
    assert health.metadata_json["empty_streak"] == 1
    assert "zero usable items" in health.last_error


def test_huggingface_sha_is_a_revision_but_not_automatically_material(session):
    spec = source("hf", entity="openai", kind="huggingface_models", evidence="official_repo")
    sync_source(session, spec)
    first = CollectedItem(
        source_id=spec.id,
        external_id="org/model",
        canonical_url="https://huggingface.co/org/model",
        title="org/model",
        summary="text-generation",
        metadata={"sha": "aaa"},
    )
    row, changed = ingest_item(session, spec, first)
    assert changed
    row, changed = ingest_item(session, spec, first.model_copy(update={"metadata": {"sha": "bbb"}}))
    assert changed
    assert row.metadata_json["update_status"] == "MINOR_UPDATE"


def test_acceptance_and_code_artifacts_are_material_updates(session):
    spec = source("openreview", entity="openreview", kind="openreview_api", evidence="paper")
    sync_source(session, spec)
    first = CollectedItem(
        source_id=spec.id,
        external_id="paper-1",
        canonical_url="https://openreview.net/forum?id=paper-1",
        title="Long-Horizon Autonomous Agent",
        summary="agent memory experiments",
        metadata={"acceptance_status": "accepted"},
    )
    ingest_item(session, spec, first)
    row, changed = ingest_item(
        session,
        spec,
        first.model_copy(
            update={
                "metadata": {
                    "acceptance_status": "camera_ready",
                    "code_url": "https://github.com/example/paper-1",
                }
            }
        ),
    )
    assert changed
    assert row.metadata_json["update_status"] == "MATERIAL_UPDATE"


def test_arxiv_and_conference_record_for_same_paper_share_one_cluster(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    arxiv = source("arxiv-cross", entity="arxiv", kind="arxiv_api", evidence="paper")
    conference = source(
        "openreview-cross", entity="openreview", kind="openreview_api", evidence="paper"
    )
    for spec, external_id, url, metadata in (
        (
            arxiv,
            "2607.12345",
            "https://arxiv.org/abs/2607.12345v1",
            {"version": 1, "authors": ["A. Author"], "arxiv_id": "2607.12345"},
        ),
        (
            conference,
            "forum-1",
            "https://openreview.net/forum?id=forum-1",
            {"acceptance_status": "accepted"},
        ),
    ):
        sync_source(session, spec)
        ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=external_id,
                canonical_url=url,
                title="Persistent Memory for Long-Horizon Autonomous Agents",
                summary="long-horizon autonomous agent memory and planning experiments",
                entity_id=spec.entity_id,
                evidence_type="paper",
                metadata=metadata,
            ),
        )
        enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")
    events = session.scalars(select(RadarEventModel)).all()
    assert len(events) == 2
    assert sum(event.cluster_id == event.id for event in events) == 1
    assert sum(event.is_public for event in events) == 1


def test_seed_enrichment_is_offline_and_company_claim(session):
    seed = json.loads((ROOT / "tests/fixtures/touch_high_seed.json").read_text())
    spec = source("touch-high", entity="zhipu", kind="html", evidence="official_company")
    sync_source(session, spec)
    ingest_item(
        session,
        spec,
        CollectedItem(
            source_id=spec.id,
            external_id=seed["source_id"],
            canonical_url=seed["canonical_url"],
            title=seed["title"],
            content=seed["content"],
            published_at=seed["published_at"],
            evidence_type="official_company",
        ),
    )
    result = enrich_pending(
        session,
        classifier=RuleTopicClassifier.from_config(ROOT / "configs"),
        qwen=None,
        config_dir=ROOT / "configs",
    )
    event = session.scalar(select(RadarEventModel))
    assert result["processed"] == 1
    assert event is not None
    assert set(event.topics) >= set(seed["expected_topics"])
    assert event.verification_status == "company_claim"
    assert session.scalar(select(func.count()).select_from(EventRevisionModel)) == 1
    assert len(session.scalar(select(ItemVersionModel)).embedding) == 1024

    class FakeQwenPlus:
        enabled = True
        summarizer_model = "qwen-plus"

        def summarize(self, item, topics):
            return QwenResult(
                topics=topics,
                title_zh="Touch High 四条技术主线",
                summary_zh="官方叙事与可验证事实需要分层。",
                why_it_matters="避免把公司主张当作独立验证。",
            )

    edited = editorialize_top(session, qwen=FakeQwenPlus(), limit=1)
    repeated = editorialize_top(session, qwen=FakeQwenPlus(), limit=1)
    assert edited == {"attempted": 1, "updated": 1, "failed": 0}
    assert repeated == {"attempted": 0, "updated": 0, "failed": 0}
    assert event.title_zh == "Touch High 四条技术主线"


def test_semantic_duplicate_becomes_corroboration_not_second_public_card(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    text = "Autonomous Agent System multi-agent tool use runtime and continual learning"
    first = source("official-a", entity="openai")
    second = source("official-b", entity="openai")
    for spec, suffix in ((first, "a"), (second, "b")):
        sync_source(session, spec)
        ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=suffix,
                canonical_url=f"https://example.com/{suffix}",
                title="Autonomous Agent Runtime",
                summary=text,
                entity_id="openai",
                evidence_type="official_company",
            ),
        )
        enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")

    events = session.scalars(select(RadarEventModel).order_by(RadarEventModel.created_at)).all()
    assert len(events) == 2
    assert sum(event.is_public for event in events) == 1
    canonical = next(event for event in events if event.is_public)
    assert canonical.corroborating_urls[0]["url"] == "https://example.com/b"
    supporting = session.scalars(
        select(EventItemModel).where(
            EventItemModel.event_id == canonical.id, EventItemModel.relation == "supports"
        )
    ).all()
    assert len(supporting) == 1


def test_primary_evidence_promotes_an_unconfirmed_cluster(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    media = source("media", entity="openai", kind="rss", evidence="reputable_media")
    filing = source("filing", entity="openai", kind="sec_submissions", evidence="regulatory_filing")
    text = "OpenAI RAISE financing for artificial intelligence infrastructure and GPU compute"
    for spec, url in ((media, "https://media.example/report"), (filing, "https://sec.gov/filing")):
        sync_source(session, spec)
        ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=spec.id,
                canonical_url=url,
                title="OpenAI financing round",
                summary=text,
                entity_id="openai",
                evidence_type=spec.evidence_type,
            ),
        )
        enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")

    canonical = session.scalar(
        select(RadarEventModel).where(
            RadarEventModel.primary_url == "https://sec.gov/filing",
            RadarEventModel.is_public.is_(True),
        )
    )
    assert canonical is not None
    assert canonical.is_public is True
    assert canonical.verification_status == "corroborated"
    assert canonical.status == "MATERIAL_UPDATE"
    revisions = session.scalars(
        select(EventRevisionModel).where(EventRevisionModel.event_id == canonical.id)
    ).all()
    assert len(revisions) == 2

    # Evidence promotion owns an event-level revision number. A later update
    # from the original item must allocate the next number, not reuse its own
    # item-level revision counter.
    ingest_item(
        session,
        media,
        CollectedItem(
            source_id=media.id,
            external_id=media.id,
            canonical_url="https://media.example/report",
            title="OpenAI financing round updated",
            summary=f"{text} with updated transaction terms",
            entity_id="openai",
            evidence_type=media.evidence_type,
        ),
    )
    enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")
    session.flush()
    revision_numbers = session.scalars(
        select(EventRevisionModel.revision_no)
        .where(EventRevisionModel.event_id == canonical.id)
        .order_by(EventRevisionModel.revision_no)
    ).all()
    assert revision_numbers == [1, 2, 3]
    assert canonical.primary_url == "https://sec.gov/filing"
    assert canonical.verification_status == "corroborated"
    assert canonical.is_public is True

    ingest_item(
        session,
        filing,
        CollectedItem(
            source_id=filing.id,
            external_id=filing.id,
            canonical_url="https://sec.gov/filing",
            title="OpenAI financing final official terms",
            summary=f"{text} with final official transaction terms",
            entity_id="openai",
            evidence_type=filing.evidence_type,
        ),
    )
    enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")
    session.flush()
    assert canonical.primary_url == "https://sec.gov/filing"
    assert canonical.title_zh == "OpenAI financing final official terms"
    assert canonical.verification_status == "corroborated"
    roots = session.scalars(
        select(RadarEventModel).where(RadarEventModel.cluster_id == RadarEventModel.id)
    ).all()
    assert [root.id for root in roots] == [canonical.id]


def test_company_claim_and_media_combination_is_order_independent():
    from ai_research_radar.contracts import VerificationStatus

    reported = VerificationStatus.REPORTED_UNCONFIRMED
    claim = VerificationStatus.COMPANY_CLAIM
    assert _combined_verification(reported, claim) == claim
    assert _combined_verification(claim, reported) == claim


def test_two_media_sources_detect_same_issuer_and_form_observation_cluster(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    for source_id, media_entity, url in (
        ("media-one", "media_reuters", "https://media-one.example/openai-round"),
        ("media-two", "media_ft", "https://media-two.example/openai-round"),
    ):
        spec = source(
            source_id,
            entity=media_entity,
            kind="rss",
            evidence="reputable_media",
        )
        sync_source(session, spec)
        ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=source_id,
                canonical_url=url,
                title="OpenAI financing round for AI infrastructure",
                summary="OpenAI raised financing for GPU data center infrastructure",
                entity_id=media_entity,
                evidence_type="reputable_media",
            ),
        )
        enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")
    canonical = session.scalar(
        select(RadarEventModel).where(RadarEventModel.cluster_id == RadarEventModel.id)
    )
    assert canonical is not None
    assert canonical.entities == ["openai"]
    assert canonical.verification_status == "reported_unconfirmed"
    assert canonical.is_public is False
    assert len(canonical.corroborating_urls) == 1


def test_sec_periodic_results_are_digestible_but_routine_form4_stays_archive_only(session):
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    spec = source("sec", entity="alphabet", kind="sec_submissions", evidence="regulatory_filing")
    sync_source(session, spec)
    for native_id, form, routine in (
        ("10q", "10-Q", False),
        ("form4", "4", True),
    ):
        ingest_item(
            session,
            spec,
            CollectedItem(
                source_id=spec.id,
                external_id=native_id,
                canonical_url=f"https://www.sec.gov/Archives/{native_id}",
                title=f"Alphabet · {form}",
                summary=(
                    "AI model revenue guidance and generative AI outlook"
                    if form == "10-Q"
                    else "Official filing"
                ),
                entity_id="alphabet",
                evidence_type="regulatory_filing",
                metadata={"form": form, "routine": routine},
            ),
        )
    enrich_pending(session, classifier=classifier, config_dir=ROOT / "configs")
    events = {event.event_type: event for event in session.scalars(select(RadarEventModel)).all()}
    assert events["EARNINGS_GUIDANCE"].is_public is True
    assert "OWNERSHIP" not in events
