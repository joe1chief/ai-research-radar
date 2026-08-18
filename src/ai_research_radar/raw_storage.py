"""Private, short-lived raw snapshot storage for production collection."""

from __future__ import annotations

import gzip
import re
from datetime import UTC, date, datetime
from urllib.parse import quote

import httpx


class RawSnapshotStore:
    """Minimal Supabase Storage adapter; no storage credential reaches collectors."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        bucket: str = "radar-raw",
        max_bytes: int = 5 * 1024 * 1024,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = supabase_url.rstrip("/")
        self.bucket = bucket
        self.max_bytes = max_bytes
        self._owns_client = client is None
        self._auth_headers = {
            "apikey": secret_key,
            "authorization": f"Bearer {secret_key}",
        }
        self.client = client or httpx.Client(timeout=30)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def put(
        self,
        *,
        source_id: str,
        item_id: str,
        content_hash: str,
        payload: bytes,
        fetched_at: datetime,
    ) -> str | None:
        if not payload or len(payload) > self.max_bytes:
            return None
        stamp = fetched_at.astimezone(UTC)
        path = (
            f"{stamp:%Y/%m/%d}/{_safe(source_id)}/{_safe(item_id)}/"
            f"{content_hash}.html.gz"
        )
        compressed = gzip.compress(payload, compresslevel=6, mtime=0)
        url = (
            f"{self.base_url}/storage/v1/object/{quote(self.bucket, safe='')}/"
            f"{quote(path, safe='/')}"
        )
        response = self.client.post(
            url,
            content=compressed,
            headers={
                **self._auth_headers,
                "content-type": "application/gzip",
                "x-upsert": "false",
                "cache-control": "private, max-age=0, no-store",
            },
        )
        # A deterministic object path makes an already-existing snapshot a
        # successful idempotent replay rather than a collection failure.
        if response.status_code != 409:
            response.raise_for_status()
        return path

    def delete(self, paths: list[str]) -> None:
        if not paths:
            return
        url = f"{self.base_url}/storage/v1/object/{quote(self.bucket, safe='')}"
        for offset in range(0, len(paths), 100):
            response = self.client.request(
                "DELETE",
                url,
                json={"prefixes": paths[offset : offset + 100]},
                headers=self._auth_headers,
            )
            response.raise_for_status()

    def list_older_than(self, cutoff: date) -> list[str]:
        """Find expired objects by date prefixes, including DB-orphaned uploads."""

        expired: list[str] = []
        for year in self._list_prefix(""):
            if not year["name"].isdigit() or len(year["name"]) != 4:
                continue
            year_prefix = year["name"]
            for month in self._list_prefix(year_prefix):
                month_prefix = f"{year_prefix}/{month['name']}"
                for day in self._list_prefix(month_prefix):
                    try:
                        prefix_date = date.fromisoformat(
                            f"{year_prefix}-{month['name']}-{day['name']}"
                        )
                    except ValueError:
                        continue
                    if prefix_date < cutoff:
                        expired.extend(self._walk_objects(f"{month_prefix}/{day['name']}"))
        return expired

    def _list_prefix(self, prefix: str) -> list[dict]:
        url = f"{self.base_url}/storage/v1/object/list/{quote(self.bucket, safe='')}"
        entries: list[dict] = []
        for offset in range(0, 100_000, 1000):
            response = self.client.post(
                url,
                json={
                    "prefix": prefix,
                    "limit": 1000,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
                headers=self._auth_headers,
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                raise ValueError("Supabase Storage list response must be a list")
            entries.extend(value for value in page if isinstance(value, dict) and value.get("name"))
            if len(page) < 1000:
                break
        return entries

    def _walk_objects(self, prefix: str, depth: int = 0) -> list[str]:
        if depth > 8:
            return []
        paths: list[str] = []
        for entry in self._list_prefix(prefix):
            path = f"{prefix}/{entry['name']}"
            if entry.get("id") or entry.get("metadata") is not None:
                paths.append(path)
            else:
                paths.extend(self._walk_objects(path, depth + 1))
        return paths


def _safe(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:120] or "unknown"
