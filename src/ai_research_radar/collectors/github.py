"""GitHub release collector; ordinary commits are intentionally out of scope."""

from __future__ import annotations

from ..contracts import CollectedItem, CollectionBatch
from ..identity import canonicalize_url
from .base import BaseCollector
from .parsing import parse_datetime


class GitHubReleaseCollector(BaseCollector):
    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        payload = response.json()
        if not isinstance(payload, list):
            payload = []
        items: list[CollectedItem] = []
        for release in payload:
            if release.get("draft"):
                continue
            release_id = str(release.get("id") or release.get("tag_name") or "")
            url = release.get("html_url")
            if not release_id or not url:
                continue
            items.append(
                CollectedItem(
                    source_id=self.spec.id,
                    external_id=release_id,
                    canonical_url=canonicalize_url(str(url)),
                    title=str(release.get("name") or release.get("tag_name") or release_id),
                    summary=str(release.get("body") or ""),
                    published_at=parse_datetime(release.get("published_at")),
                    updated_at=parse_datetime(release.get("updated_at")),
                    entity_id=self.spec.entity_id,
                    evidence_type=self.spec.evidence_type,
                    metadata={
                        "tag_name": release.get("tag_name"),
                        "prerelease": bool(release.get("prerelease")),
                        "author": (release.get("author") or {}).get("login"),
                    },
                )
            )
        return CollectionBatch(items=items, cursor=next_cursor)
