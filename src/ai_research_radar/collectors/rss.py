"""Generic RSS/Atom collector for official research and company feeds."""

from __future__ import annotations

import feedparser

from ..contracts import CollectedItem, CollectionBatch
from ..identity import canonicalize_url, stable_id
from .base import BaseCollector
from .parsing import parse_datetime


class RSSCollector(BaseCollector):
    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        parsed = feedparser.parse(response.content)
        max_items = max(1, int(getattr(self.spec, "max_items", 500)))
        items: list[CollectedItem] = []
        for entry in parsed.entries[:max_items]:
            raw_url = str(entry.get("link") or "")
            if not raw_url:
                continue
            canonical = canonicalize_url(raw_url)
            external_id = str(entry.get("id") or entry.get("guid") or stable_id(canonical))
            content = "\n".join(str(part.get("value", "")) for part in entry.get("content", []))
            items.append(
                CollectedItem(
                    source_id=self.spec.id,
                    external_id=external_id,
                    canonical_url=canonical,
                    title=str(entry.get("title", "")),
                    summary=str(entry.get("summary", "")),
                    content=content,
                    authors=[str(author.get("name")) for author in entry.get("authors", [])],
                    published_at=parse_datetime(entry.get("published_parsed") or entry.get("published")),
                    updated_at=parse_datetime(entry.get("updated_parsed") or entry.get("updated")),
                    entity_id=self.spec.entity_id,
                    evidence_type=self.spec.evidence_type,
                    metadata={"feed_title": parsed.feed.get("title", "")},
                )
            )
        warnings = []
        if getattr(parsed, "bozo", False):
            warnings.append(f"feed parse warning: {getattr(parsed, 'bozo_exception', '')}")
        return CollectionBatch(items=items, cursor=next_cursor, warnings=warnings)
