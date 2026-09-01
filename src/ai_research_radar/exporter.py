"""Sanitized static export consumed by the public Vite application and RSS readers."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import load_issuers
from .db import EventRevisionModel, RadarEventModel, SourceHealthModel, SourceModel, utcnow
from .identity import normalize_content

WEB_TOPICS = {
    "long_horizon",
    "autonomous_agent",
    "self_evolving",
    "mechanistic_interpretability",
    "safety_governance",
    "industrial_capital",
    "podcast_culture",
}


def export_public_dataset(
    session: Session,
    output: Path,
    *,
    config_dir: Path | str = "configs",
    timezone: str = "Asia/Shanghai",
    window_days: int = 30,
) -> dict[str, Any]:
    threshold = utcnow() - timedelta(days=window_days)
    all_rows = session.scalars(
        select(RadarEventModel).where(
            RadarEventModel.is_public.is_(True),
            RadarEventModel.archived_at.is_(None),
            RadarEventModel.verification_status != "reported_unconfirmed",
        ).order_by(RadarEventModel.source_time.desc(), RadarEventModel.score.desc())
    ).all()
    names = _entity_names(config_dir)
    all_events = [_public_event(session, event, names) for event in all_rows]

    # Compute lineage / related events for each event
    for event in all_events:
        event_entities = {e["id"] for e in event.get("entities", [])}
        related = [
            {
                "event_id": other["event_id"],
                "title_zh": other["title_zh"],
                "published_at": other["published_at"],
                "score": other["score"],
            }
            for other in all_events
            if other["event_id"] != event["event_id"]
            and (
                (event_entities and event_entities.intersection({e["id"] for e in other.get("entities", [])}))
                or (other.get("cluster_id") and other.get("cluster_id") == event.get("cluster_id"))
            )
        ][:4]
        event["related_events"] = related

    events = [
        event
        for event in all_events
        if max(
            _parse_iso(event["first_seen_at"]),
            _parse_iso(event["material_updated_at"])
            if event.get("material_updated_at")
            else _parse_iso(event["first_seen_at"]),
        )
        >= threshold
    ]
    health_rows = session.scalars(select(SourceHealthModel)).all()
    healthy = sum(row.status == "healthy" for row in health_rows)
    degraded = sum(row.status not in {"healthy", "disabled"} for row in health_rows)
    successes = [row.last_success_at for row in health_rows if row.last_success_at]
    generated_at = _iso(utcnow())
    dataset = {
        "schema_version": "1.0",
        "public_export": True,
        "generated_at": generated_at,
        "timezone": timezone,
        "window_days": window_days,
        "source_health": {
            "healthy": healthy,
            "degraded": degraded,
            "last_success_at": _iso(max(successes)) if successes else None,
            "notices": [
                f"{row.source_id}:source_{row.status}"
                for row in health_rows
                if row.status not in {"healthy", "disabled"}
            ][:10],
        },
        "events": events,
        "facets": _facets(events),
    }
    _assert_public_payload(dataset)
    _write_json(output, dataset)

    # Export RSS 2.0 / Atom XML feed
    rss_xml = _generate_rss_xml(events, timezone)
    feed_path = output.parent / "feed.xml"
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    feed_path.write_text(rss_xml, encoding="utf-8")
    if output.parent.name == "data":
        # Also mirror to web/public/feed.xml
        root_feed_path = output.parent.parent / "feed.xml"
        if root_feed_path.parent.exists():
            root_feed_path.write_text(rss_xml, encoding="utf-8")

    month_dir = output.parent / "months"
    by_month: dict[str, list[dict[str, Any]]] = {}
    for event in all_events:
        by_month.setdefault(event["published_at"][:7], []).append(event)
    for month, month_events in by_month.items():
        monthly = {
            **dataset,
            "window_days": None,
            "events": month_events,
            "facets": _facets(month_events),
        }
        _assert_public_payload(monthly)
        _write_json(month_dir / f"{month}.json", monthly)
    month_index = {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "months": [
            {"month": month, "count": len(by_month[month])}
            for month in sorted(by_month, reverse=True)
        ],
    }
    _assert_public_payload(month_index)
    _write_json(month_dir / "index.json", month_index)
    return dataset


def _clean_prose(text: str | None) -> str:
    if not text:
        return ""
    text = normalize_content(text)
    text = re.sub(r"(?s)@[a-zA-Z0-9_-]+\s+[^{]+\{(?:[^{}]*\{[^{}]*\}[^{}]*|[^{}]*)*\}", " ", text)
    for _ in range(2):
        text = re.sub(r"(?s)(?:[.#@][a-zA-Z0-9_\-]+[a-zA-Z0-9_\-\.\#\:\s+>~,]*|[a-zA-Z0-9_\-]+\s*:)\s*\{[^{}]*\}", " ", text)
        text = re.sub(r"\{[^{}]*:[^{}]*\}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _public_event(session: Session, event: RadarEventModel, names: dict[str, str]) -> dict[str, Any]:
    revisions = session.scalars(
        select(EventRevisionModel)
        .where(
            EventRevisionModel.event_id == event.id,
            EventRevisionModel.is_material.is_(True),
        )
        .order_by(EventRevisionModel.revision_no.asc())
    ).all()
    revision = revisions[-1] if revisions else None
    snapshot = revision.snapshot if revision else {}
    published = event.source_time or event.first_seen_at
    topics = [topic for topic in event.topics if topic in WEB_TOPICS]
    if "mechanistic_interpretability" in event.topics and "safety_governance" not in topics:
        topics.append("safety_governance")
    evidence_type = _web_evidence_type(event, snapshot)
    paper_links = {
        key: snapshot.get(f"{key}_url")
        for key in ("arxiv", "alphaxiv", "code", "project")
        if snapshot.get(f"{key}_url")
    }
    timeline = [
        {"at": _iso(published), "label": "来源发布", "detail": event.title_zh, "kind": "source"},
        {
            "at": _iso(event.first_seen_at),
            "label": "Radar 首次发现",
            "detail": event.change_summary or "首次收录",
            "kind": "discovery",
        },
    ]
    if event.material_updated_at:
        for material in revisions[1:]:
            material_snapshot = material.snapshot or {}
            timeline.append(
                {
                    "at": _iso(material.created_at),
                    "label": "实质更新",
                    "detail": material_snapshot.get("change_summary")
                    or "核心内容或证据链更新",
                    "kind": "update",
                }
            )

    key_quotes = snapshot.get("key_quotes") or []
    deep_takeaway = snapshot.get("deep_takeaway") or ""

    return {
        "event_id": event.id,
        "cluster_id": event.cluster_id,
        "event_type": event.event_type,
        "topics": topics,
        "entities": [
            {
                "id": entity,
                "name": (
                    entity.removeprefix("author:")
                    if entity.startswith("author:")
                    else names.get(entity, entity)
                ),
                "kind": _entity_kind(entity, names),
            }
            for entity in event.entities
        ],
        "title_zh": _clean_prose(event.title_zh),
        "summary_zh": _clean_prose(event.summary_zh),
        "why_it_matters": _clean_prose(event.why_it_matters),
        "change_summary": _clean_prose(event.change_summary),
        "key_quotes": [_clean_prose(q) for q in (key_quotes or []) if _clean_prose(q)],
        "deep_takeaway": _clean_prose(deep_takeaway),
        "source_time": _iso(published),
        "published_at": _iso(published),
        "first_seen_at": _iso(event.first_seen_at),
        "material_updated_at": _iso(event.material_updated_at) if event.material_updated_at else None,
        "status": event.status,
        "source_type": event.source_type,
        "verification_status": event.verification_status,
        "evidence_type": evidence_type,
        "score": event.score,
        "primary_url": event.primary_url,
        "corroborating_urls": event.corroborating_urls or [],
        "paper_links": paper_links or None,
        "deep_read": snapshot.get("alphaxiv_insight"),
        "tags": event.cross_tags or [],
        "timeline": timeline,
    }


def _generate_rss_xml(events: list[dict[str, Any]], timezone: str) -> str:
    items_xml = []
    for event in events[:60]:
        pub_dt = _parse_iso(event["published_at"])
        pub_rfc822 = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0800")
        title = f"[{event['score']}分] {event['title_zh']}"
        link = event["primary_url"]
        guid = event["event_id"]
        topics_str = " · ".join(event.get("topics", []))
        summary = event.get("summary_zh", "")
        why = event.get("why_it_matters", "")
        takeaway = event.get("deep_takeaway", "")
        quotes = "".join(f"<li>{q}</li>" for q in event.get("key_quotes", []))
        quotes_html = f"<p><strong>【核心金句/关键机制】</strong></p><ul>{quotes}</ul>" if quotes else ""
        takeaway_html = f"<p><strong>【核心洞察】</strong> {takeaway}</p>" if takeaway else ""
        content_html = (
            f"<p><strong>【深度研判】</strong> {summary}</p>"
            f"<p><strong>【为什么重要】</strong> {why}</p>"
            f"{takeaway_html}"
            f"{quotes_html}"
            f"<p><a href='{link}'>查看一手信源 &rarr;</a></p>"
        )
        items_xml.append(f"""    <item>
      <title><![CDATA[{title}]]></title>
      <link>{link}</link>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub_rfc822}</pubDate>
      <category>{topics_str}</category>
      <description><![CDATA[{summary}]]></description>
      <content:encoded><![CDATA[{content_html}]]></content:encoded>
    </item>""")

    build_date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>AI Research Radar | 硅谷与全球 AI 原生独角兽前沿情报</title>
    <link>https://joe1chief.github.io/ai-research-radar/</link>
    <description>全天候追踪硅谷 AI-Native 独角兽、前沿推理架构、自主智能体与顶流深度播客的专业情报雷达。</description>
    <language>zh-CN</language>
    <lastBuildDate>{build_date}</lastBuildDate>
    <atom:link href="https://joe1chief.github.io/ai-research-radar/feed.xml" rel="self" type="application/rss+xml"/>
{"\n".join(items_xml)}
  </channel>
</rss>
"""


def _web_evidence_type(event: RadarEventModel, snapshot: dict[str, Any]) -> str:
    evidence = str(snapshot.get("evidence_type") or "")
    if evidence == "paper" or event.source_type in {
        "arxiv_api",
        "arxiv_oai",
        "openreview_api",
        "acl_anthology",
        "pmlr",
    }:
        return "paper"
    if evidence in {"regulatory_filing", "exchange_filing", "official_filing"} or (
        event.source_type in {"sec_submissions", "exchange_filing"}
    ):
        return "official_filing"
    if evidence == "official_repo" or event.source_type in {
        "github_releases",
        "huggingface_models",
    }:
        return "open_source_release"
    if evidence == "reputable_media" or event.source_type in {
        "reputable_media",
        "sitemap",
        "rss",
    }:
        return "reputable_media"
    return "official_company"


def _entity_names(config_dir: Path | str) -> dict[str, str]:
    issuers = load_issuers(Path(config_dir))
    return {
        issuer["id"]: (issuer.get("name_zh") or issuer.get("name_en") or issuer["id"])
        for issuer in issuers
        if isinstance(issuer, dict) and "id" in issuer
    }


def _entity_kind(entity: str, names: dict[str, str]) -> str:
    if entity.startswith("author:"):
        return "author_group"
    if entity in names:
        return "issuer"
    if entity in {"mcp", "a2a", "terminal_bench", "gaia"}:
        return "project"
    return "company"


def _facets(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    topics = Counter()
    entities = Counter()
    event_types = Counter()
    evidence_types = Counter()
    verification = Counter()
    statuses = Counter()
    for event in events:
        topics.update(event.get("topics", []))
        entities.update(entity["id"] for entity in event.get("entities", []))
        event_types[event.get("event_type", "")] += 1
        evidence_types[event.get("evidence_type", "")] += 1
        verification[event.get("verification_status", "")] += 1
        statuses[event.get("status", "")] += 1
    return {
        "topics": dict(topics),
        "entities": dict(entities),
        "event_types": dict(event_types),
        "evidence_types": dict(evidence_types),
        "verification_statuses": dict(verification),
        "statuses": dict(statuses),
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


FORBIDDEN_PUBLIC_KEYS = {
    "recipient",
    "recipient_email",
    "email",
    "raw_html",
    "raw_content",
    "prompt",
    "system_prompt",
    "delivery",
    "deliveries",
    "delivery_state",
    "agentmail_draft_id",
    "message_id",
    "api_key",
    "secret",
}


def _assert_public_payload(value: Any, path: str = "dataset") -> None:
    """Fail the build before a private field or unsafe link reaches Pages."""

    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public_payload(item, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if key.casefold() in FORBIDDEN_PUBLIC_KEYS:
            raise ValueError(f"public export contains forbidden field: {path}.{key}")
        if key in {"primary_url", "url", "arxiv", "alphaxiv", "code", "project"}:
            if child is not None and not _is_http_url(str(child)):
                raise ValueError(f"public export contains unsafe URL: {path}.{key}")
        _assert_public_payload(child, f"{path}.{key}")


def _is_http_url(value: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    return parts.scheme.casefold() in {"http", "https"} and bool(parts.hostname)


def _iso(value: datetime) -> str:
    """Serialize SQLite-naive UTC and Postgres-aware timestamps identically."""

    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
