from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from ai_research_radar.db import ItemModel, ItemVersionModel, SourceModel, session_factory
from ai_research_radar.maintenance import readonly_session
from ai_research_radar.retention import estimate_retention, RetentionInspectionUnavailable


def seed(session):
    now = datetime(2026, 10, 10, tzinfo=UTC)
    session.add(
        SourceModel(
            id="s",
            entity_id="s",
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
            id="i",
            source_id="s",
            native_id="i",
            canonical_url="https://example.invalid/i",
            item_type="article",
            title="i",
            current_content_hash="current",
        )
    )
    session.flush()
    for n, days in [
        ("current", 200),
        ("previous", 100),
        ("candidate", 110),
        ("evidence", 120),
        ("event", 130),
        ("young", 10),
    ]:
        # Previous means most recent non-current; young is therefore also the latest history.
        session.add(
            ItemVersionModel(
                id=n,
                item_id="i",
                version_key=n,
                content_hash=n,
                title=n,
                fetched_at=now - timedelta(days=days),
                abstract_text="中",
                normalized_text="正文",
                embedding=[0.0] * 1024,
                embedding_vector=[0.0] * 1024,
                metadata_json={"safe": "kept"},
            )
        )
    session.commit()
    session.execute(
        text("CREATE TABLE evidence (item_version_id TEXT REFERENCES item_versions(id))")
    )
    session.execute(text("INSERT INTO evidence VALUES ('evidence')"))
    # Only FK fixture shape is needed: production association remains unmodified.
    session.execute(
        text(
            "INSERT INTO events (id,cluster_id,event_type,topics,entities,cross_tags,title_zh,summary_zh,why_it_matters,first_seen_at,material_updated_at,status,source_type,verification_status,score,primary_url,corroborating_urls,is_public,delivery_suppressed,created_at,updated_at) VALUES ('e','e','NEWS','[]','[]','[]','t','s','w',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'NEW_ENTITY','rss','verified_primary',1,'https://example.invalid','[]',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    )
    session.execute(
        text("INSERT INTO event_items VALUES ('e','event','primary',CURRENT_TIMESTAMP)")
    )
    session.commit()
    return now


def test_retention_readonly_protects_current_references_and_utf8_bytes(session):
    now = seed(session)
    factory = session_factory(session.get_bind())
    with readonly_session(factory) as reader:
        result = estimate_retention(reader, preview=True, now=now, limit=1)
    assert result["available"] and result["candidate_count"] == 2
    assert result["retention_reason_counts"]["current"] == 1
    assert result["retention_reason_counts"]["latest_history"] == 1
    assert result["retention_reason_counts"]["evidence_reference"] == 1
    assert result["retention_reason_counts"]["event_reference"] == 1
    assert result["candidate_logical_payload_bytes"] == 2 * (9 + 8192)
    assert result["preview_truncated"] and len(result["candidates"]) == 1
    assert result["physical_reclaimable_bytes"] is None and not result["apply_available"]
    assert (
        session.scalar(
            select(ItemVersionModel.normalized_text).where(ItemVersionModel.id == "candidate")
        )
        == "正文"
    )
    assert session.scalar(select(ItemModel.current_content_hash)) == "current"
    assert result["items_by_version_count"]["three_or_more"] == 1


def test_latest_history_is_retained_even_over_90_days(session):
    now = seed(session)
    session.execute(text("DELETE FROM item_versions WHERE id='young'"))
    session.commit()
    result = estimate_retention(session, preview=True, now=now)
    assert "previous" not in {r["version_id"] for r in result["candidates"]}
    assert result["candidate_count"] == 1


def test_unknown_foreign_key_fails_closed(session):
    now = seed(session)
    session.execute(
        text("CREATE TABLE additional_evidence (version_id TEXT REFERENCES item_versions(id))")
    )
    session.commit()
    with pytest.raises(RetentionInspectionUnavailable, match="references"):
        estimate_retention(session, now=now)


def test_missing_current_fails_closed(session):
    now = seed(session)
    session.execute(text("UPDATE items SET current_content_hash='missing'"))
    session.commit()
    with pytest.raises(RetentionInspectionUnavailable, match="current"):
        estimate_retention(session, now=now)


def test_age_cutoff_exclusive_and_hash_metadata_unchanged(session):
    now = seed(session)
    session.execute(
        text("UPDATE item_versions SET fetched_at=:stamp WHERE id='candidate'"),
        {"stamp": now - timedelta(days=90)},
    )
    session.commit()
    result = estimate_retention(session, now=now)
    assert result["candidate_count"] == 1
    assert session.get(ItemVersionModel, "candidate").metadata_json == {"safe": "kept"}
