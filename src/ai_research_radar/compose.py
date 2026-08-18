"""Digest/alert selection and AgentMail outbox composition."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime, time
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .contracts import DeliveryState, VerificationStatus
from .db import (
    DeliveryEventRevisionModel,
    DeliveryModel,
    EventItemModel,
    EventRevisionModel,
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    SourceHealthModel,
    SourceModel,
    utcnow,
)
from .scoring import alert_eligible


TOPIC_ORDER = [
    "long_horizon",
    "autonomous_agent",
    "self_evolving",
    "mechanistic_interpretability",
    "safety_governance",
    "industrial_capital",
]

TOPIC_LABELS = {
    "long_horizon": "Long Horizon Task / 长程任务",
    "autonomous_agent": "Autonomous Agent System / 自治智能体系统",
    "self_evolving": "Fully Self Training / Self-Evolving",
    "mechanistic_interpretability": "机械可解释性",
    "safety_governance": "极致安全治理 / Safety & Governance",
    "industrial_capital": "Industrial Capital / 产业资本",
}

DELIVERABLE_REVISION_STATUSES = {
    "NEW_ENTITY",
    "MATERIAL_UPDATE",
    "DISCOVERED_LATE",
}


def compose_delivery(
    session: Session,
    *,
    digest_date: date,
    recipient: str | None,
    timezone: str = "Asia/Shanghai",
    kind: str = "digest",
) -> DeliveryModel | None:
    recipient_value = recipient or "shadow@example.invalid"
    recipient_hash = hashlib.sha256(recipient_value.casefold().encode()).hexdigest()
    known_delivery_key = (
        f"digest:{recipient_hash}:{digest_date.isoformat()}" if kind == "digest" else None
    )
    events, revisions = _events_for_delivery(
        session,
        digest_date,
        timezone,
        delivery_kind=kind,
        current_delivery_key=known_delivery_key,
    )
    snapshots = {revision.event_id: revision.snapshot for revision in revisions.values()}

    if kind == "alert":
        eligible: list[RadarEventModel] = []
        for event in events:
            verification = VerificationStatus(event.verification_status)
            revision = next(
                (value for value in revisions.values() if value.event_id == event.id), None
            )
            if (
                revision
                and alert_eligible(
                    event.score,
                    verification,
                    event.event_type,
                    paper_has_release_evidence=bool(
                        (revision.snapshot or {}).get("code_url")
                        or (revision.snapshot or {}).get("project_url")
                        or event.corroborating_urls
                    ),
                )
            ):
                eligible.append(event)
        events = sorted(eligible, key=lambda event: event.score, reverse=True)[:5]
        selected_event_ids = {event.id for event in events}
        revisions = {
            revision_id: revision
            for revision_id, revision in revisions.items()
            if revision.event_id in selected_event_ids
        }
        snapshots = {revision.event_id: revision.snapshot for revision in revisions.values()}
        if not events:
            return None
        suffix = hashlib.sha256("|".join(sorted(revisions)).encode()).hexdigest()[:20]
        delivery_key = f"alert:{recipient_hash}:{suffix}"
        subject = f"[AI Radar 重大预警] {events[0].title_zh if events else '无符合条件事件'}"
        send_at = None
    else:
        delivery_key = known_delivery_key
        assert delivery_key is not None
        subject = f"AI Research Radar 日报 · {digest_date.isoformat()}"
        send_at = datetime.combine(
            digest_date, time(13, 45), tzinfo=ZoneInfo(timezone)
        ).astimezone(UTC)

    health = _source_health(session)
    text, html = render_message(
        events,
        digest_date,
        kind=kind,
        snapshots=snapshots,
        source_health=health,
    )
    revision_ids = sorted(revisions)
    existing = session.get(DeliveryModel, delivery_key)
    if existing is not None:
        previous_ids = sorted((existing.metadata_json or {}).get("event_revision_ids", []))
        mutable_states = {
            DeliveryState.PENDING.value,
            DeliveryState.DRAFT.value,
            DeliveryState.SHADOW.value,
            DeliveryState.SCHEDULED.value,
        }
        send_at_existing = existing.send_at
        if send_at_existing and send_at_existing.tzinfo is None:
            send_at_existing = send_at_existing.replace(tzinfo=UTC)
        still_mutable = (
            existing.state in mutable_states
            and (send_at_existing is None or send_at_existing > utcnow())
        )
        content_changed = (
            previous_ids != revision_ids
            or (existing.metadata_json or {}).get("text") != text
            or (existing.metadata_json or {}).get("html") != html
        )
        if still_mutable and content_changed:
            existing.metadata_json = {
                **(existing.metadata_json or {}),
                "subject": subject,
                "text": text,
                "html": html,
                "event_revision_ids": revision_ids,
            }
            session.execute(
                delete(DeliveryEventRevisionModel).where(
                    DeliveryEventRevisionModel.delivery_key == delivery_key
                )
            )
            for revision_id in revision_ids:
                session.add(
                    DeliveryEventRevisionModel(
                        delivery_key=delivery_key,
                        event_revision_id=revision_id,
                    )
                )
            existing.state = DeliveryState.PENDING.value
            existing.updated_at = utcnow()
            session.flush()
        return existing
    row = DeliveryModel(
        delivery_key=delivery_key,
        recipient_hash=recipient_hash,
        channel="agentmail",
        delivery_kind=kind,
        send_at=send_at,
        state="pending",
        metadata_json={
            "subject": subject,
            "text": text,
            "html": html,
            "event_revision_ids": revision_ids,
        },
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    for revision_id in revision_ids:
        session.add(
            DeliveryEventRevisionModel(
                delivery_key=delivery_key,
                event_revision_id=revision_id,
            )
        )
    session.flush()
    return row


def ensure_operations_delivery(
    session: Session,
    *,
    digest_date: date,
    recipient: str | None,
    timezone: str = "Asia/Shanghai",
) -> DeliveryModel | None:
    """Create one 14:07 operations notice when the daily path is still unhealthy."""

    recipient_value = recipient or "shadow@example.invalid"
    recipient_hash = hashlib.sha256(recipient_value.casefold().encode()).hexdigest()
    digest_key = f"digest:{recipient_hash}:{digest_date.isoformat()}"
    digest = session.get(DeliveryModel, digest_key)
    healthy_states = {
        DeliveryState.SENT.value,
        DeliveryState.DELIVERED.value,
    }
    if digest is not None and digest.state in healthy_states:
        return None
    issue = "日报记录不存在" if digest is None else f"日报投递状态为 {digest.state}"
    delivery_key = f"operations:{recipient_hash}:{digest_date.isoformat()}"
    existing = session.get(DeliveryModel, delivery_key)
    if existing is not None:
        return existing
    subject = f"[AI Radar 运维异常] {digest_date.isoformat()} 日报未完成"
    text = (
        f"AI Research Radar 运维异常 · {digest_date.isoformat()}\n\n"
        f"{issue}。系统已保持幂等停机保护，不会盲目重发。\n\n"
        "请检查 GitHub Actions、AgentMail Draft/消息标签、Webhook 与来源健康状态。"
    )
    html = (
        "<!doctype html><html><body><h1>AI Research Radar 运维异常</h1>"
        f"<p>{escape(issue)}。</p><p>系统已保持幂等停机保护，不会盲目重发。</p>"
        "<p>请检查 GitHub Actions、AgentMail Draft/消息标签、Webhook 与来源健康状态。</p>"
        "</body></html>"
    )
    row = DeliveryModel(
        delivery_key=delivery_key,
        recipient_hash=recipient_hash,
        channel="agentmail",
        delivery_kind="operations",
        send_at=None,
        state=DeliveryState.PENDING.value,
        metadata_json={
            "subject": subject,
            "text": text,
            "html": html,
            "event_revision_ids": [],
        },
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _events_for_delivery(
    session: Session,
    target: date,
    timezone: str,
    *,
    delivery_kind: str,
    current_delivery_key: str | None = None,
) -> tuple[list[RadarEventModel], dict[str, EventRevisionModel]]:
    """Select every still-undelivered material revision up to this digest run.

    Calendar-day filtering loses events discovered after the daily compose job.
    The delivery ledger is the authoritative cursor instead: a revision may be
    selected once for alerts and once for digests, independently.
    """

    zone = ZoneInfo(timezone)
    end_of_target = datetime.combine(target, time.max, tzinfo=zone).astimezone(UTC)
    cutoff = min(utcnow(), end_of_target)
    used_query = (
        select(DeliveryEventRevisionModel.event_revision_id)
        .join(
            DeliveryModel,
            DeliveryModel.delivery_key == DeliveryEventRevisionModel.delivery_key,
        )
        .where(DeliveryModel.delivery_kind == delivery_kind)
    )
    if current_delivery_key is not None:
        used_query = used_query.where(DeliveryModel.delivery_key != current_delivery_key)
    used_revision_ids = set(session.scalars(used_query).all())
    rows = session.scalars(
        select(RadarEventModel)
        .where(
            RadarEventModel.archived_at.is_(None),
            RadarEventModel.delivery_suppressed.is_(False),
        )
        .order_by(RadarEventModel.score.desc())
    ).all()
    result: list[RadarEventModel] = []
    revisions: dict[str, EventRevisionModel] = {}
    for event in rows:
        revision = session.scalar(
            select(EventRevisionModel)
            .where(
                EventRevisionModel.event_id == event.id,
                EventRevisionModel.status.in_(DELIVERABLE_REVISION_STATUSES),
                EventRevisionModel.created_at <= cutoff,
            )
            .order_by(EventRevisionModel.revision_no.desc())
        )
        if revision is None or revision.id in used_revision_ids:
            continue
        independently_reported = (
            event.verification_status == VerificationStatus.REPORTED_UNCONFIRMED.value
            and _has_two_independent_media_sources(session, event)
        )
        if (
            event.score >= 45
            and (event.is_public or independently_reported)
        ):
            result.append(event)
            revisions[revision.id] = revision
    return result, revisions


def _latest_revisions(session: Session, events: list[RadarEventModel]) -> dict[str, EventRevisionModel]:
    result: dict[str, EventRevisionModel] = {}
    for event in events:
        revision = session.scalar(
            select(EventRevisionModel)
            .where(
                EventRevisionModel.event_id == event.id,
                EventRevisionModel.status.in_(DELIVERABLE_REVISION_STATUSES),
            )
            .order_by(EventRevisionModel.revision_no.desc())
        )
        if revision is not None:
            result[revision.id] = revision
    return result


def _has_two_independent_media_sources(
    session: Session, event: RadarEventModel
) -> bool:
    source_ids = set(
        session.scalars(
            select(SourceModel.id)
            .join(ItemModel, ItemModel.source_id == SourceModel.id)
            .join(ItemVersionModel, ItemVersionModel.item_id == ItemModel.id)
            .join(EventItemModel, EventItemModel.item_version_id == ItemVersionModel.id)
            .where(
                EventItemModel.event_id == event.id,
                SourceModel.evidence_type == "reputable_media",
            )
        ).all()
    )
    if source_ids:
        return len(source_ids) >= 2
    # ORM-only fixtures and imported legacy rows may lack evidence relations.
    # In that narrow case, require different publisher domains—not merely URLs.
    domains = {
        domain
        for domain in (
            _publisher_domain(event.primary_url),
            *(
                _publisher_domain(str(link.get("url") or ""))
                for link in (event.corroborating_urls or [])
            ),
        )
        if domain
    }
    return len(domains) >= 2


def _publisher_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in {"co.uk", "com.cn", "com.hk", "com.au", "co.jp", "org.cn"}:
        return ".".join(labels[-3:])
    return suffix


def render_message(
    events: list[RadarEventModel],
    target: date,
    *,
    kind: str,
    snapshots: dict[str, dict] | None = None,
    source_health: dict[str, int] | None = None,
) -> tuple[str, str]:
    snapshots = snapshots or {}
    source_health = source_health or {"healthy": 0, "degraded": 0, "failing": 0}
    if not events:
        text = (
            f"AI Research Radar · {target.isoformat()}\n\n今日无高可信新增。\n\n"
            f"数据源健康状态：正常 {source_health['healthy']} · "
            f"降级 {source_health['degraded']} · 失败 {source_health['failing']}"
        )
        html = (
            "<!doctype html><html><body><h1>AI Research Radar</h1>"
            f"<p>{target.isoformat()}</p><p>今日无高可信新增。</p>"
            f"<h2>数据源健康状态</h2><p>正常 {source_health['healthy']} · "
            f"降级 {source_health['degraded']} · 失败 {source_health['failing']}</p>"
            "</body></html>"
        )
        return text, html

    confirmed = [
        event
        for event in events
        if event.verification_status != VerificationStatus.REPORTED_UNCONFIRMED.value
    ]
    unconfirmed = [
        event
        for event in events
        if event.verification_status == VerificationStatus.REPORTED_UNCONFIRMED.value
    ]
    groups: dict[str, list[RadarEventModel]] = defaultdict(list)
    for event in confirmed:
        for topic in event.topics:
            groups[topic].append(event)
    top = sorted(confirmed, key=lambda event: event.score, reverse=True)[:3]
    text_parts = [f"AI Research Radar · {target.isoformat()}", "", "今日最重要的 3 件事"]
    for index, event in enumerate(top, 1):
        text_parts.append(f"{index}. [{event.score}] {event.title_zh} — {event.primary_url}")
    html_parts = [
        "<!doctype html><html><body>",
        f"<h1>{'重大预警' if kind == 'alert' else 'AI Research Radar 日报'}</h1>",
        f"<p>{target.isoformat()}</p>",
        "<h2>今日最重要的 3 件事</h2><ol>",
    ]
    for event in top:
        html_parts.append(
            f'<li><a href="{escape(event.primary_url, quote=True)}">{escape(event.title_zh)}</a> '
            f"<strong>{event.score}</strong></li>"
        )
    html_parts.append("</ol>")
    seen_sections: set[str] = set()
    for topic in TOPIC_ORDER:
        section_events = groups.get(topic, [])
        if not section_events:
            continue
        # Mechanistic cards also carry the safety parent; avoid a duplicate safety section entry.
        html_parts.append(f"<h2>{escape(TOPIC_LABELS[topic])}</h2>")
        text_parts.extend(["", TOPIC_LABELS[topic]])
        for event in section_events:
            # Mechanistic interpretability is the dedicated child section;
            # don't repeat its automatically-added safety parent card.
            if topic == "safety_governance" and "mechanistic_interpretability" in event.topics:
                continue
            marker = event.id
            if marker in seen_sections:
                continue
            seen_sections.add(marker)
            snapshot = snapshots.get(event.id, {})
            html_parts.append(_render_card(event, snapshot))
            text_parts.extend(
                [
                    f"- [{event.score}] {event.title_zh}",
                    f"  发生了什么：{event.summary_zh}",
                    f"  为什么归入该主题：{_topic_rationale(event)}",
                    f"  为什么重要：{event.why_it_matters}",
                    *(
                        [f"  alphaXiv 深读：{snapshot['alphaxiv_insight'].get('summary', '')}"]
                        if snapshot.get("alphaxiv_insight")
                        else []
                    ),
                    f"  变化：{event.change_summary or '首次收录'}",
                    f"  证据：{event.verification_status} / {_evidence_type(event, snapshot)}",
                    f"  时间：来源 {event.source_time.isoformat() if event.source_time else '未知'} · 首次发现 {event.first_seen_at.isoformat()}",
                    f"  一级链接：{event.primary_url}",
                ]
            )
            text_parts.extend(_text_links(event, snapshot))
    html_parts.append("<h2>待官方确认</h2>")
    text_parts.extend(["", "待官方确认"])
    if unconfirmed:
        for event in unconfirmed:
            html_parts.append(_render_card(event, snapshots.get(event.id, {})))
            text_parts.extend(
                [
                    f"- [{event.score}] {event.title_zh}",
                    f"  两个来源的报道线索，尚无一级披露：{event.primary_url}",
                    *_text_links(event, snapshots.get(event.id, {})),
                ]
            )
    else:
        html_parts.append("<p>无满足双来源条件的待确认事件。</p>")
        text_parts.append("无满足双来源条件的待确认事件。")
    html_parts.append("<h2>数据源健康状态</h2>")
    html_parts.append(
        f"<p>正常 {source_health['healthy']} · 降级 {source_health['degraded']} · "
        f"失败 {source_health['failing']}</p>"
    )
    text_parts.extend(
        [
            "",
            "数据源健康状态",
            f"正常 {source_health['healthy']} · 降级 {source_health['degraded']} · 失败 {source_health['failing']}",
        ]
    )
    html_parts.append("</body></html>")
    return "\n".join(text_parts), "".join(html_parts)


def _render_card(event: RadarEventModel, snapshot: dict) -> str:
    source_time = event.source_time.isoformat() if event.source_time else "未知"
    links = _html_links(event, snapshot)
    deep_read = snapshot.get("alphaxiv_insight") or {}
    deep_read_html = (
        f"<p><strong>alphaXiv 深读：</strong>{escape(str(deep_read.get('summary', '')))}</p>"
        if deep_read
        else ""
    )
    return (
        '<article style="border:1px solid #ddd;padding:16px;margin:12px 0">'
        f'<h3><a href="{escape(event.primary_url, quote=True)}">{escape(event.title_zh)}</a></h3>'
        f"<p><strong>发生了什么：</strong>{escape(event.summary_zh)}</p>"
        f"<p><strong>为什么归入该主题：</strong>{escape(_topic_rationale(event))}</p>"
        f"<p><strong>为什么重要：</strong>{escape(event.why_it_matters)}</p>"
        f"{deep_read_html}"
        f"<p><strong>与上次相比：</strong>{escape(event.change_summary or '首次收录')}</p>"
        f"<p>证据：{escape(event.verification_status)} / "
        f"{escape(_evidence_type(event, snapshot))} · "
        f"来源/事件时间：{escape(source_time)} · 首次发现：{escape(event.first_seen_at.isoformat())}</p>"
        f"<p>{links}</p></article>"
    )


def _evidence_type(event: RadarEventModel, snapshot: dict) -> str:
    return str(snapshot.get("evidence_type") or event.source_type)


def _topic_rationale(event: RadarEventModel) -> str:
    labels = [TOPIC_LABELS.get(topic, topic) for topic in event.topics]
    return "事件内容或正式事件类型命中：" + "、".join(labels)


def _link_pairs(event: RadarEventModel, snapshot: dict) -> list[tuple[str, str]]:
    pairs = [("一级来源", event.primary_url)]
    for key, label in (
        ("arxiv_url", "arXiv"),
        ("alphaxiv_url", "alphaXiv"),
        ("code_url", "代码"),
        ("project_url", "项目页"),
    ):
        value = snapshot.get(key)
        if value and value != event.primary_url:
            pairs.append((label, str(value)))
    for link in event.corroborating_urls or []:
        url = link.get("url")
        if url and url != event.primary_url:
            pairs.append((str(link.get("label") or "佐证"), str(url)))
    seen: set[str] = set()
    return [(label, url) for label, url in pairs if not (url in seen or seen.add(url))]


def _html_links(event: RadarEventModel, snapshot: dict) -> str:
    return " · ".join(
        f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
        for label, url in _link_pairs(event, snapshot)
    )


def _text_links(event: RadarEventModel, snapshot: dict) -> list[str]:
    extras = [
        f"{label} {url}"
        for label, url in _link_pairs(event, snapshot)
        if label != "一级来源"
    ]
    return [f"  相关链接：{' · '.join(extras)}"] if extras else []


def _source_health(session: Session) -> dict[str, int]:
    rows = session.scalars(select(SourceHealthModel)).all()
    return {
        "healthy": sum(row.status == "healthy" for row in rows),
        "degraded": sum(row.status in {"degraded", "unknown"} for row in rows),
        "failing": sum(row.status == "failing" for row in rows),
    }
