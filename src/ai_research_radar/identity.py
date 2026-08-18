"""Deterministic identities and hashes used by every incremental path."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def normalize_content(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parts.path)
    if path != "/":
        path = path.rstrip("/")
    netloc = parts.netloc.lower()
    scheme = parts.scheme.lower() or "https"
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def content_hash(value: str) -> str:
    return hashlib.sha256(normalize_content(value).encode("utf-8")).hexdigest()


def stable_id(*parts: str) -> str:
    joined = "\x1f".join(normalize_content(part).lower() for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def stable_uuid(*parts: str) -> str:
    """A deterministic UUID suitable for the Postgres UUID primary keys."""

    return str(uuid.UUID(stable_id(*parts)[:32]))


ARXIV_RE = re.compile(r"(?P<id>(?:[a-z-]+/\d{7}|\d{4}\.\d{4,5}))(?:v(?P<version>\d+))?", re.I)


def parse_arxiv_identity(value: str) -> tuple[str, int]:
    match = ARXIV_RE.search(value)
    if not match:
        raise ValueError(f"not an arXiv identity: {value}")
    return match.group("id").lower(), int(match.group("version") or 1)
