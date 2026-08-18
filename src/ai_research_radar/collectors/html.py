"""Conservative generic HTML listing collector for first-party index pages."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..contracts import CollectedItem, CollectionBatch
from ..identity import canonicalize_url, normalize_content, stable_id
from .base import BaseCollector, CollectorHTTPError, _same_site
from .parsing import parse_datetime


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, normalize_content(" ".join(self._parts))))
            self._href = None
            self._parts = []


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.jsonld: list[str] = []
        self._capture_depth = 0
        self._script_jsonld = False
        self._script_parts: list[str] = []
        self._title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag in {"article", "main"}:
            self._capture_depth += 1
        elif self._capture_depth and tag in {"p", "h1", "h2", "h3", "li", "blockquote"}:
            self.text_parts.append("\n")
        if tag == "meta":
            key = values.get("property") or values.get("name") or values.get("itemprop")
            content = values.get("content")
            if key and content:
                self.meta[key.casefold()] = content
        elif tag == "script" and "ld+json" in values.get("type", "").casefold():
            self._script_jsonld = True
            self._script_parts = []
        elif tag == "title":
            self._title = True
            self._title_parts = []
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"])

    def handle_data(self, data: str) -> None:
        if self._script_jsonld:
            self._script_parts.append(data)
        elif self._title:
            self._title_parts.append(data)
        if self._capture_depth:
            self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"article", "main"} and self._capture_depth:
            self._capture_depth -= 1
        elif tag == "script" and self._script_jsonld:
            self.jsonld.append("".join(self._script_parts))
            self._script_jsonld = False
        elif tag == "title" and self._title:
            self.meta.setdefault("title", normalize_content(" ".join(self._title_parts)))
            self._title = False

class HtmlListingCollector(BaseCollector):
    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        parser = _LinkParser()
        parser.feed(response.text)
        source_host = (urlsplit(self.spec.url).hostname or "").lower()
        seen: set[str] = set()
        items: list[CollectedItem] = []
        for href, title in parser.links:
            if not href or href.startswith(("#", "javascript:", "mailto:")) or len(title) < 4:
                continue
            absolute = canonicalize_url(urljoin(self.spec.url, href))
            target_host = (urlsplit(absolute).hostname or "").lower()
            if not _same_site(source_host, target_host) or absolute == canonicalize_url(self.spec.url):
                continue
            if not _listing_allowed(self.spec, absolute, title):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            items.append(_listing_item(self.spec, absolute, title))

        max_items = max(1, int(getattr(self.spec, "max_items", 500)))
        items = items[:max_items]

        detail_limit = max(0, int(getattr(self.spec, "detail_fetch_limit", 0)))
        warnings: list[str] = []
        if detail_limit:
            ranked = sorted(items, key=_detail_rank, reverse=True)
            enriched: dict[str, CollectedItem] = {item.canonical_url: item for item in items}
            fresh_count = max(1, detail_limit // 2)
            rotating_count = max(0, detail_limit - fresh_count)
            rotating_pool = ranked[fresh_count:]
            offset = int((cursor or {}).get("detail_offset", 0))
            rotating = [
                rotating_pool[(offset + index) % len(rotating_pool)]
                for index in range(min(rotating_count, len(rotating_pool)))
            ] if rotating_pool else []
            selected = list(
                {item.canonical_url: item for item in [*ranked[:fresh_count], *rotating]}.values()
            )
            if rotating_pool:
                next_cursor["detail_offset"] = (offset + len(rotating)) % len(rotating_pool)
            for item in selected:
                try:
                    detail_response = self.request({}, url=item.canonical_url)
                    if "html" not in detail_response.headers.get("content-type", "text/html"):
                        continue
                    enriched[item.canonical_url] = _detail_item(
                        self.spec,
                        item,
                        detail_response.text,
                        str(detail_response.url),
                    )
                except (CollectorHTTPError, httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    warnings.append(f"detail fetch failed for {item.canonical_url}: {exc}")
            items = list(enriched.values())
        return CollectionBatch(items=items, cursor=next_cursor, warnings=warnings[:10])


def _listing_item(spec, absolute: str, title: str) -> CollectedItem:
    routine_patterns = list(getattr(spec, "routine_title_patterns", []))
    return CollectedItem(
        source_id=spec.id,
        external_id=stable_id(absolute),
        canonical_url=absolute,
        title=title,
        entity_id=spec.entity_id,
        evidence_type=spec.evidence_type,
        metadata={
            "listing_url": spec.url,
            "routine": any(
                re.search(pattern, title, flags=re.IGNORECASE)
                for pattern in routine_patterns
            ),
        },
    )


def _listing_allowed(spec, url: str, title: str) -> bool:
    include_urls = list(getattr(spec, "include_url_patterns", []))
    exclude_urls = list(getattr(spec, "exclude_url_patterns", []))
    include_titles = list(getattr(spec, "include_title_patterns", []))
    exclude_titles = list(getattr(spec, "exclude_title_patterns", []))
    if include_urls and not any(re.search(pattern, url, re.IGNORECASE) for pattern in include_urls):
        return False
    if include_titles and not any(
        re.search(pattern, title, re.IGNORECASE) for pattern in include_titles
    ):
        return False
    if any(re.search(pattern, url, re.IGNORECASE) for pattern in exclude_urls):
        return False
    if any(re.search(pattern, title, re.IGNORECASE) for pattern in exclude_titles):
        return False
    return True


def _detail_rank(item: CollectedItem) -> tuple[int, int]:
    value = f"{item.canonical_url} {item.title}".casefold()
    positive = sum(
        term in value
        for term in ("blog", "research", "news", "article", "post", "release", "model", "safety", "paper")
    )
    negative = sum(
        term in value
        for term in ("about", "career", "contact", "privacy", "terms", "login", "signup", "cookie")
    )
    return (positive * 10 - negative * 20, -len(item.canonical_url))


def _detail_item(spec, listing: CollectedItem, html: str, response_url: str) -> CollectedItem:
    parser = _ArticleParser()
    parser.feed(html)
    structured: dict[str, Any] = {}
    for raw in parser.jsonld:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and isinstance(candidate.get("@graph"), list):
                candidates.extend(candidate["@graph"])
            if not isinstance(candidate, dict):
                continue
            kind = candidate.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(item).casefold() in {"article", "newsarticle", "blogposting", "report", "scholarlyarticle"} for item in kinds):
                structured = candidate
                break
        if structured:
            break
    canonical = _safe_detail_canonical(
        str(structured.get("url") or parser.meta.get("og:url") or response_url),
        fallback=listing.canonical_url,
        source_url=spec.url,
    )
    title = normalize_content(
        str(
            structured.get("headline")
            or parser.meta.get("og:title")
            or parser.meta.get("twitter:title")
            or listing.title
            or parser.meta.get("title", "")
        )
    )
    description = normalize_content(
        str(
            structured.get("description")
            or parser.meta.get("description")
            or parser.meta.get("og:description")
            or ""
        )
    )
    content = normalize_content(" ".join(parser.text_parts))[:50000]
    published = (
        structured.get("datePublished")
        or parser.meta.get("article:published_time")
        or parser.meta.get("datepublished")
    )
    modified = (
        structured.get("dateModified")
        or parser.meta.get("article:modified_time")
        or parser.meta.get("datemodified")
    )
    native_id = structured.get("@id") or structured.get("identifier") or canonical
    authors_value = structured.get("author") or []
    if not isinstance(authors_value, list):
        authors_value = [authors_value]
    authors = [
        normalize_content(str(author.get("name") if isinstance(author, dict) else author))
        for author in authors_value
        if author
    ]
    links = [
        normalized
        for link in parser.links
        if (normalized := _safe_http_url(urljoin(canonical, link))) is not None
    ]
    code_url = next(
        (
            link
            for link in links
            if (urlsplit(link).hostname or "").casefold()
            in {"github.com", "www.github.com", "gitlab.com", "www.gitlab.com"}
        ),
        None,
    )
    return CollectedItem(
        source_id=spec.id,
        external_id=listing.external_id,
        canonical_url=canonical,
        title=title or listing.title,
        summary=description,
        content=content or description,
        authors=authors,
        published_at=parse_datetime(published),
        updated_at=parse_datetime(modified),
        entity_id=spec.entity_id,
        evidence_type=spec.evidence_type,
        metadata={
            "listing_url": spec.url,
            "source_native_id": str(native_id),
            "jsonld_type": structured.get("@type"),
            "code_url": code_url,
        },
        raw_snapshot=html.encode("utf-8"),
    )


def _safe_http_url(value: str) -> str | None:
    normalized = canonicalize_url(value)
    parts = urlsplit(normalized)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    return normalized


def _safe_detail_canonical(candidate: str, *, fallback: str, source_url: str) -> str:
    normalized = _safe_http_url(candidate)
    source_host = (urlsplit(source_url).hostname or "").casefold()
    target_host = (urlsplit(normalized).hostname or "").casefold() if normalized else ""
    if normalized and _same_site(source_host, target_host):
        return normalized
    return fallback
