from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_research_radar.compose import (
    compose_delivery,
    ensure_operations_delivery,
    render_message,
)
from ai_research_radar.db import (
    DeliveryModel,
    DeliveryEventRevisionModel,
    EventRevisionModel,
    RadarEventModel,
)
from ai_research_radar.exporter import export_public_dataset
from ai_research_radar.identity import stable_id


ROOT = Path(__file__).parents[2]


def event(now):
    event_id = stable_id("event", "public")
    return RadarEventModel(
        id=event_id,
        cluster_id=event_id,
        event_type="MODEL_RELEASE",
        topics=["autonomous_agent"],
        entities=["openai"],
        cross_tags=["ai_native"],
        title_zh="自治智能体运行时发布",
        summary_zh="官方发布了新的多智能体运行时。",
        why_it_matters="它更新了工具调用和多智能体协作能力。",
        change_summary="首次收录",
        source_time=now,
        first_seen_at=now,
        status="NEW_ENTITY",
        source_type="github_releases",
        verification_status="verified_primary",
        score=88,
        primary_url="https://github.com/example/release",
        corroborating_urls=[],
        is_public=True,
        created_at=now,
        updated_at=now,
    )


def test_empty_digest_is_explicit_health_signal():
    text, html = render_message([], datetime(2026, 7, 12).date(), kind="digest")
    assert "今日无高可信新增" in text
    assert "今日无高可信新增" in html


def test_email_card_contains_topic_reasoning_and_all_paper_links():
    now = datetime.now(UTC)
    model = event(now)
    model.event_type = "PAPER"
    text, html = render_message(
        [model],
        now.date(),
        kind="digest",
        snapshots={
            model.id: {
                "arxiv_url": "https://arxiv.org/abs/2607.00001",
                "alphaxiv_url": "https://alphaxiv.org/abs/2607.00001",
                "code_url": "https://github.com/example/code",
                "project_url": "https://example.com/project",
            }
        },
    )
    assert "为什么归入该主题" in text
    assert "alphaXiv" in html
    assert "https://github.com/example/code" in html
    assert "https://example.com/project" in html


def test_compose_is_idempotent_and_export_matches_web_contract(session, tmp_path):
    now = datetime.now(UTC)
    model = event(now)
    session.add(model)
    session.flush()
    revision = EventRevisionModel(
        id=stable_id(model.id, "1"),
        event_id=model.id,
        revision_no=1,
        content_hash="b" * 64,
        status="NEW_ENTITY",
        is_material=True,
        snapshot={
            "arxiv_url": "https://arxiv.org/abs/2501.12345",
            "alphaxiv_url": "https://alphaxiv.org/abs/2501.12345",
        },
    )
    session.add(revision)
    session.flush()
    delivery = compose_delivery(
        session,
        digest_date=now.astimezone().date(),
        recipient="private@example.com",
    )
    same = compose_delivery(
        session,
        digest_date=now.astimezone().date(),
        recipient="private@example.com",
    )
    assert delivery.delivery_key == same.delivery_key

    # SQLite drops timezone metadata on round-trip; the public contract must
    # still emit explicit UTC rather than browser-local naive timestamps.
    session.expire_all()
    output = tmp_path / "latest.json"
    dataset = export_public_dataset(session, output, config_dir=ROOT / "configs")
    exported = json.loads(output.read_text())
    assert dataset["public_export"] is True
    assert exported["events"][0]["entities"][0] == {
        "id": "openai",
        "name": "OpenAI",
        "kind": "issuer",
    }
    assert exported["events"][0]["paper_links"]["alphaxiv"].endswith("2501.12345")
    assert exported["events"][0]["published_at"].endswith("Z")
    assert exported["events"][0]["first_seen_at"].endswith("Z")
    assert "facets" in exported
    month_index = json.loads((output.parent / "months" / "index.json").read_text())
    assert month_index["months"][0]["month"] == exported["events"][0]["published_at"][:7]
    serialized = output.read_text()
    assert "private@example.com" not in serialized
    assert "recipient_hash" not in serialized


def test_digest_excludes_single_media_rumor_but_allows_two_source_watch_item(session):
    now = datetime.now(UTC)
    rumor = event(now)
    rumor.id = stable_id("event", "rumor")
    rumor.cluster_id = rumor.id
    rumor.is_public = False
    rumor.verification_status = "reported_unconfirmed"
    rumor.score = 60
    rumor.primary_url = "https://media-one.example/report"
    session.add(rumor)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(rumor.id, "1"),
            event_id=rumor.id,
            revision_no=1,
            content_hash="c" * 64,
            status="NEW_ENTITY",
            is_material=True,
            snapshot={},
        )
    )
    session.flush()
    target = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    first = compose_delivery(session, digest_date=target, recipient="one@example.com")
    assert rumor.title_zh not in first.metadata_json["text"]

    rumor.corroborating_urls = [
        {"label": "第二家独立媒体", "url": "https://media-two.example/report"}
    ]
    rumor.status = "MATERIAL_UPDATE"
    rumor.material_updated_at = now
    session.add(
        EventRevisionModel(
            id=stable_id(rumor.id, "2"),
            event_id=rumor.id,
            revision_no=2,
            content_hash="d" * 64,
            status="MATERIAL_UPDATE",
            is_material=True,
            snapshot={},
        )
    )
    session.flush()
    second = compose_delivery(session, digest_date=target, recipient="two@example.com")
    assert "待官方确认" in second.metadata_json["text"]
    assert rumor.title_zh in second.metadata_json["text"]


def test_same_publisher_urls_do_not_satisfy_two_media_gate(session):
    now = datetime.now(UTC)
    rumor = event(now)
    rumor.id = stable_id("event", "same-publisher")
    rumor.cluster_id = rumor.id
    rumor.is_public = False
    rumor.verification_status = "reported_unconfirmed"
    rumor.score = 70
    rumor.primary_url = "https://news.example.com/report-one"
    rumor.corroborating_urls = [
        {"label": "同一出版方另一篇", "url": "https://www.example.com/report-two"}
    ]
    session.add(rumor)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(rumor.id, "1"),
            event_id=rumor.id,
            revision_no=1,
            content_hash="7" * 64,
            status="NEW_ENTITY",
            is_material=True,
            snapshot={},
        )
    )
    session.flush()
    target = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    digest = compose_delivery(session, digest_date=target, recipient="same@example.com")
    assert rumor.title_zh not in digest.metadata_json["text"]


def test_next_digest_picks_up_unused_revision_from_previous_calendar_day(session):
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    model = event(yesterday)
    model.id = stable_id("event", "after-yesterday-digest")
    model.cluster_id = model.id
    model.score = 70
    session.add(model)
    session.flush()
    revision = EventRevisionModel(
        id=stable_id(model.id, "1"),
        event_id=model.id,
        revision_no=1,
        content_hash="6" * 64,
        status="NEW_ENTITY",
        is_material=True,
        snapshot={},
        created_at=yesterday,
    )
    session.add(revision)
    session.flush()
    target = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    digest = compose_delivery(session, digest_date=target, recipient="incremental@example.com")
    assert model.title_zh in digest.metadata_json["text"]

    tomorrow = target + timedelta(days=1)
    next_digest = compose_delivery(
        session,
        digest_date=tomorrow,
        recipient="incremental@example.com",
    )
    assert model.title_zh not in next_digest.metadata_json["text"]


def test_digest_can_refresh_same_scheduled_draft_before_send(session):
    now = datetime.now(UTC)
    first_event = event(now)
    session.add(first_event)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(first_event.id, "refresh-1"),
            event_id=first_event.id,
            revision_no=1,
            content_hash="e" * 64,
            status="NEW_ENTITY",
            is_material=True,
            snapshot={},
        )
    )
    session.flush()
    target = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    delivery = compose_delivery(session, digest_date=target, recipient="refresh@example.com")
    delivery.state = "scheduled"
    delivery.send_at = now + timedelta(hours=1)

    second_event = event(now)
    second_event.id = stable_id("event", "second-refresh")
    second_event.cluster_id = second_event.id
    second_event.title_zh = "第二条恢复后的高可信事件"
    session.add(second_event)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(second_event.id, "refresh-1"),
            event_id=second_event.id,
            revision_no=1,
            content_hash="f" * 64,
            status="NEW_ENTITY",
            is_material=True,
            snapshot={},
        )
    )
    session.flush()
    refreshed = compose_delivery(session, digest_date=target, recipient="refresh@example.com")
    assert refreshed.delivery_key == delivery.delivery_key
    assert refreshed.state == "pending"
    assert "第二条恢复后的高可信事件" in refreshed.metadata_json["text"]
    assert len(
        session.query(DeliveryEventRevisionModel)
        .filter(DeliveryEventRevisionModel.delivery_key == delivery.delivery_key)
        .all()
    ) == 2


def test_flagship_paper_with_code_can_alert(session):
    now = datetime.now(UTC)
    paper = event(now)
    paper.id = stable_id("event", "flagship-paper")
    paper.cluster_id = paper.id
    paper.event_type = "PAPER"
    paper.score = 90
    session.add(paper)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(paper.id, "paper-1"),
            event_id=paper.id,
            revision_no=1,
            content_hash="1" * 64,
            status="NEW_ENTITY",
            is_material=True,
            snapshot={"code_url": "https://github.com/example/flagship"},
        )
    )
    session.flush()
    alert = compose_delivery(
        session,
        digest_date=now.astimezone(ZoneInfo("Asia/Shanghai")).date(),
        recipient="paper-alert@example.com",
        kind="alert",
    )
    assert alert is not None
    assert alert.delivery_kind == "alert"


def test_operations_notice_is_idempotent_and_suppressed_after_delivery(session):
    target = datetime(2026, 7, 12, tzinfo=UTC).date()
    recipient = "ops@example.com"
    notice = ensure_operations_delivery(
        session,
        digest_date=target,
        recipient=recipient,
    )
    same = ensure_operations_delivery(
        session,
        digest_date=target,
        recipient=recipient,
    )
    assert notice is not None
    assert same is not None
    assert same.delivery_key == notice.delivery_key
    assert notice.delivery_kind == "operations"

    digest = compose_delivery(session, digest_date=target, recipient=recipient)
    assert digest is not None
    digest.state = "delivered"
    session.flush()
    assert (
        ensure_operations_delivery(
            session,
            digest_date=target,
            recipient=recipient,
        )
        is None
    )
    assert session.query(DeliveryModel).filter_by(delivery_kind="operations").count() == 1


def test_archive_backfill_event_is_public_on_web_but_suppressed_from_email(session, tmp_path):
    now = datetime.now(UTC)
    model = event(now)
    model.delivery_suppressed = True
    session.add(model)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(model.id, "suppressed-backfill"),
            event_id=model.id,
            revision_no=1,
            content_hash="9" * 64,
            status="NEW_ENTITY",
            is_material=True,
            snapshot={"evidence_type": "official_repo"},
        )
    )
    session.flush()
    target = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    delivery = compose_delivery(session, digest_date=target, recipient="backfill@example.com")
    assert model.title_zh not in delivery.metadata_json["text"]

    output = tmp_path / "latest.json"
    exported = export_public_dataset(session, output, config_dir=ROOT / "configs")
    assert exported["events"][0]["event_id"] == model.id


def test_latest_export_keeps_old_event_with_recent_material_update(session, tmp_path):
    now = datetime.now(UTC)
    model = event(now - timedelta(days=90))
    model.id = stable_id("event", "old-but-updated")
    model.cluster_id = model.id
    model.material_updated_at = now
    model.status = "MATERIAL_UPDATE"
    model.source_type = "openreview_api"
    session.add(model)
    session.flush()
    session.add(
        EventRevisionModel(
            id=stable_id(model.id, "material-2"),
            event_id=model.id,
            revision_no=2,
            content_hash="8" * 64,
            status="MATERIAL_UPDATE",
            is_material=True,
            snapshot={"evidence_type": "paper"},
            created_at=now,
        )
    )
    session.flush()
    output = tmp_path / "latest.json"
    exported = export_public_dataset(session, output, config_dir=ROOT / "configs")
    assert exported["events"][0]["event_id"] == model.id
    assert exported["events"][0]["evidence_type"] == "paper"
