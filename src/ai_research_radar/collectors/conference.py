"""Official conference proceedings collectors for ACL Anthology and PMLR."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urljoin

import feedparser

from ..contracts import CollectedItem, CollectionBatch
from ..db import utcnow
from ..identity import canonicalize_url, normalize_content, stable_id
from .base import BaseCollector, CollectorHTTPError
from .parsing import parse_datetime


FRONTIER_TERMS = (
    "agent",
    "language model",
    "llm",
    "tool use",
    "computer use",
    "planning",
    "memory",
    "self-training",
    "self-play",
    "self-improv",
    "synthetic data",
    "rlvr",
    "rlaif",
    "recursive",
    "interpretability",
    "circuit",
    "activation",
    "model internals",
    "safety",
    "alignment",
    "oversight",
    "red team",
    "governance",
    "prompt injection",
    "long-horizon",
    "long horizon",
    "multi-agent",
    "autonomous",
)


def _relevant(title: str, abstract: str) -> bool:
    value = f"{title}\n{abstract}".casefold()
    return any(term in value for term in FRONTIER_TERMS)


class PMLRCollector(BaseCollector):
    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        volume_ids = sorted(
            {int(value) for value in re.findall(r'href=["\']?/?v(\d+)/?', response.text, re.I)},
            reverse=True,
        )[: int(getattr(self.spec, "volume_limit", 6))]
        items: list[CollectedItem] = []
        warnings: list[str] = []
        interval = float(getattr(self.spec, "request_interval_seconds", 0))
        for index, volume in enumerate(volume_ids):
            feed_url = f"https://proceedings.mlr.press/v{volume}/assets/rss/feed.xml"
            try:
                feed_response = self.request({}, url=feed_url)
                parsed = feedparser.parse(feed_response.content)
                for entry in parsed.entries:
                    title = normalize_content(str(entry.get("title", "")))
                    abstract = normalize_content(str(entry.get("summary", "")))
                    if not _relevant(title, abstract):
                        continue
                    canonical = canonicalize_url(str(entry.get("link") or ""))
                    if not canonical:
                        continue
                    items.append(
                        CollectedItem(
                            source_id=self.spec.id,
                            external_id=str(entry.get("id") or stable_id(canonical)),
                            canonical_url=canonical,
                            title=title,
                            summary=abstract,
                            authors=[
                                str(author.get("name"))
                                for author in entry.get("authors", [])
                                if author.get("name")
                            ],
                            published_at=parse_datetime(
                                entry.get("published_parsed") or entry.get("published")
                            ),
                            updated_at=parse_datetime(
                                entry.get("updated_parsed") or entry.get("updated")
                            ),
                            entity_id=self.spec.entity_id,
                            evidence_type=self.spec.evidence_type,
                            metadata={
                                "venue": parsed.feed.get("description", "")[:500],
                                "volume": volume,
                                "acceptance_status": "camera_ready",
                            },
                        )
                    )
            except Exception as exc:
                warnings.append(f"PMLR v{volume} feed failed: {exc}")
            if interval and index + 1 < len(volume_ids):
                self.sleep(interval)
        return CollectionBatch(items=items, cursor=next_cursor, warnings=warnings[:10])


class ACLAnthologyCollector(BaseCollector):
    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        current_year = utcnow().year
        years = {str(current_year - offset) for offset in range(int(getattr(self.spec, "years_back", 1)) + 1)}
        event_urls = []
        for path in re.findall(r'href=["\']?([^"\' >]+)', response.text, re.I):
            if not re.search(r"/events/[^/]+-(?:" + "|".join(sorted(years)) + r")/?$", path):
                continue
            url = canonicalize_url(urljoin(self.spec.url, path))
            if url not in event_urls:
                event_urls.append(url)
        event_urls = event_urls[: int(getattr(self.spec, "event_limit", 12))]
        items: dict[str, CollectedItem] = {}
        warnings: list[str] = []
        interval = float(getattr(self.spec, "request_interval_seconds", 0))
        for index, event_url in enumerate(event_urls):
            try:
                page = self.request({}, url=event_url)
                abstracts = _acl_abstracts(page.text)
                for paper_id, raw_title in re.findall(
                    r"<a[^>]*href=[\"']?/(\d{4}\.[^/\"' >]+)/?[\"']?[^>]*>(.*?)</a>",
                    page.text,
                    flags=re.I | re.S,
                ):
                    title = _strip_html(raw_title)
                    abstract = abstracts.get(paper_id, "")
                    if not _relevant(title, abstract):
                        continue
                    canonical = f"https://aclanthology.org/{paper_id}/"
                    items[paper_id] = CollectedItem(
                        source_id=self.spec.id,
                        external_id=paper_id,
                        canonical_url=canonical,
                        title=title,
                        summary=abstract,
                        entity_id=self.spec.entity_id,
                        evidence_type=self.spec.evidence_type,
                        metadata={
                            "venue_page": event_url,
                            "acceptance_status": "camera_ready",
                        },
                    )
            except Exception as exc:
                warnings.append(f"ACL event page failed for {event_url}: {exc}")
            if interval and index + 1 < len(event_urls):
                self.sleep(interval)
        return CollectionBatch(items=list(items.values()), cursor=next_cursor, warnings=warnings[:10])


def _acl_abstracts(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(
        r"id=[\"']?abstract-(\d{4})--([a-z0-9-]+)--(\d+)[\"']?[^>]*>"
        r"\s*<div[^>]*class=[\"'][^\"']*card-body[^\"']*[\"'][^>]*>(.*?)</div>",
        re.I | re.S,
    )
    for year, venue, number, raw in pattern.findall(value):
        result[f"{year}.{venue}.{number}"] = _strip_html(raw)
    return result


def _strip_html(value: str) -> str:
    return normalize_content(html_lib.unescape(re.sub(r"<[^>]+>", "", value)))
