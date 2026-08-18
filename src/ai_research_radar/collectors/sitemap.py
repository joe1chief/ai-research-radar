"""Title-only discovery from an official XML sitemap."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from ..contracts import CollectedItem, CollectionBatch
from ..identity import canonicalize_url, normalize_content, stable_id
from .base import BaseCollector
from .parsing import parse_datetime


class SitemapCollector(BaseCollector):
    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        root = ET.fromstring(response.content)
        patterns = list(getattr(self.spec, "include_url_patterns", []))
        max_items = max(1, int(getattr(self.spec, "max_items", 100)))
        items: list[CollectedItem] = []
        for node in root.findall("{*}url"):
            raw_url = node.findtext("{*}loc") or ""
            canonical = canonicalize_url(raw_url)
            if not canonical.startswith(("https://", "http://")):
                continue
            if patterns and not any(re.search(pattern, canonical, re.IGNORECASE) for pattern in patterns):
                continue
            slug = PurePosixPath(urlsplit(canonical).path).name
            slug = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", slug)
            title = normalize_content(slug.replace("-", " "))
            if len(title) < 4:
                continue
            items.append(
                CollectedItem(
                    source_id=self.spec.id,
                    external_id=stable_id(canonical),
                    canonical_url=canonical,
                    title=title,
                    updated_at=parse_datetime(node.findtext("{*}lastmod")),
                    entity_id=self.spec.entity_id,
                    evidence_type=self.spec.evidence_type,
                    metadata={"discovery_only": True, "sitemap_url": self.spec.url},
                )
            )
            if len(items) >= max_items:
                break
        return CollectionBatch(items=items, cursor=next_cursor)
