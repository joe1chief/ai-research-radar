"""arXiv Atom API collector; arXiv ID/version is the canonical paper identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser

from ..contracts import CollectedItem, CollectionBatch
from ..identity import canonicalize_url, parse_arxiv_identity
from .base import BaseCollector
from .parsing import parse_datetime


DEFAULT_QUERY = (
    "cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:cs.MA OR cat:cs.SE OR cat:cs.RO OR cat:cs.CR"
)


class ArxivCollector(BaseCollector):
    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        cursor = cursor or {}
        page_size = int(getattr(self.spec, "page_size", 100))
        max_pages = int(getattr(self.spec, "max_pages", 8))
        interval = float(getattr(self.spec, "request_interval_seconds", 0))
        query = DEFAULT_QUERY
        watermark = cursor.get("updated_at") or cursor.get("submitted_at")
        if watermark:
            try:
                start = datetime.fromisoformat(str(watermark).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                start -= timedelta(hours=12)
                query = (
                    f"({DEFAULT_QUERY}) AND lastUpdatedDate:"
                    f"[{start.astimezone(UTC).strftime('%Y%m%d%H%M%S')} TO 99991231235959]"
                )
            except ValueError:
                pass
        items_by_id: dict[str, CollectedItem] = {}
        latest: str | None = None
        warnings: list[str] = []
        next_cursor = dict(cursor)
        page_budget_reached = False
        for page in range(max_pages):
            params = {
                "search_query": query,
                "start": page * page_size,
                "max_results": page_size,
                "sortBy": "lastUpdatedDate",
                "sortOrder": "descending",
            }
            response = self.request(cursor if page == 0 else {}, params=params)
            next_cursor = self.next_cursor(response, next_cursor)
            if response.status_code == 304:
                return CollectionBatch(cursor=next_cursor, not_modified=True)
            parsed = feedparser.parse(response.content)
            warnings.extend(_bozo_warnings(parsed))
            for entry in parsed.entries:
                item = _entry_item(entry, self.spec)
                if item is None:
                    continue
                previous = items_by_id.get(item.external_id)
                if previous is None or int(item.metadata.get("version", 1)) > int(
                    previous.metadata.get("version", 1)
                ):
                    items_by_id[item.external_id] = item
                updated = item.updated_at
                if updated and (latest is None or updated.isoformat() > latest):
                    latest = updated.isoformat()
            if len(parsed.entries) < page_size:
                break
            if page + 1 == max_pages:
                try:
                    total_results = int(parsed.feed.get("opensearch_totalresults") or 0)
                except (TypeError, ValueError):
                    total_results = 0
                page_budget_reached = (
                    total_results == 0
                    or (page + 1) * page_size < total_results
                )
            if interval and page + 1 < max_pages:
                self.sleep(interval)
        if page_budget_reached:
            warnings.append(
                f"arXiv page budget reached at {max_pages} pages / "
                f"{max_pages * page_size} records; increase max_pages and replay the watermark"
            )
        if latest:
            next_cursor["updated_at"] = latest
            next_cursor.pop("submitted_at", None)
        return CollectionBatch(
            items=list(items_by_id.values()),
            cursor=next_cursor,
            warnings=list(dict.fromkeys(warnings)),
        )


def _entry_item(entry: Any, spec) -> CollectedItem | None:
    raw_id = str(entry.get("id") or entry.get("link") or "")
    try:
        arxiv_id, version = parse_arxiv_identity(raw_id)
    except ValueError:
        return None
    links = {link.get("rel", "alternate"): link.get("href") for link in entry.get("links", [])}
    canonical = links.get("alternate") or f"https://arxiv.org/abs/{arxiv_id}v{version}"
    extra_links = [link.get("href") for link in entry.get("links", []) if link.get("href")]
    code_url = next(
        (url for url in extra_links if "github.com" in url or "gitlab.com" in url), None
    )
    updated = parse_datetime(entry.get("updated_parsed") or entry.get("updated"))
    categories = [tag.get("term") for tag in entry.get("tags", []) if tag.get("term")]
    authors = [str(author.get("name")) for author in entry.get("authors", [])]
    return CollectedItem(
        source_id=spec.id,
        external_id=arxiv_id,
        canonical_url=canonicalize_url(str(canonical)),
        title=str(entry.get("title", "")),
        summary=str(entry.get("summary", "")),
        authors=authors,
        published_at=parse_datetime(entry.get("published_parsed") or entry.get("published")),
        updated_at=updated,
        entity_id=spec.entity_id,
        evidence_type=spec.evidence_type,
        metadata={
            "arxiv_id": arxiv_id,
            "version": version,
            "categories": categories,
            "authors": authors,
            "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}v{version}",
            "alphaxiv_url": f"https://alphaxiv.org/abs/{arxiv_id}",
            "code_url": code_url,
        },
    )


def _bozo_warnings(parsed: Any) -> list[str]:
    if not getattr(parsed, "bozo", False):
        return []
    return [f"feed parse warning: {getattr(parsed, 'bozo_exception', 'unknown error')}"]
