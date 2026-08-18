"""HTTP behavior shared by source collectors."""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..contracts import CollectionBatch, SourceSpec


class UnsupportedCollectorError(RuntimeError):
    pass


class CollectorHTTPError(RuntimeError):
    pass


class BaseCollector(ABC):
    def __init__(
        self,
        spec: SourceSpec,
        *,
        client: httpx.Client | None = None,
        user_agent: str = "AIResearchRadar/0.1 contact=you@example.com",
        authorization: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self.spec = spec
        self._owns_client = client is None
        default_headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, application/atom+xml, application/rss+xml, text/html;q=0.9",
        }
        if authorization:
            default_headers["Authorization"] = authorization
        self.client = client or httpx.Client(
            timeout=spec.timeout_seconds,
            follow_redirects=True,
            headers=default_headers,
        )
        self.sleep = sleep
        self.max_attempts = max_attempts

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(
        self,
        cursor: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        url: str | None = None,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        cursor = cursor or {}
        headers: dict[str, str] = {}
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])
        token = cursor.get("authorization")
        if token:
            headers["Authorization"] = str(token)
        if extra_headers:
            headers.update(extra_headers)

        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError(f"unsupported collector HTTP method: {method}")

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._request_with_safe_redirects(
                    method=method,
                    params=params,
                    data=data,
                    headers=headers,
                    url=url or self.spec.url,
                )
                if response.status_code == 304:
                    return response
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_attempts:
                        response.raise_for_status()
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt - 1)
                    self.sleep(delay + random.uniform(0, 0.25))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                self.sleep(2 ** (attempt - 1) + random.uniform(0, 0.25))
        raise CollectorHTTPError(f"failed to fetch {self.spec.id}: {last_error}") from last_error

    def _request_with_safe_redirects(
        self,
        *,
        method: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        headers: dict[str, str],
        url: str,
    ) -> httpx.Response:
        original_host = (urlsplit(url).hostname or "").lower()
        for redirect_count in range(6):
            response = self.client.request(
                method,
                url,
                params=params if redirect_count == 0 else None,
                data=data if method == "POST" else None,
                headers=headers,
                follow_redirects=False,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            target = urljoin(str(response.url), location)
            target_parts = urlsplit(target)
            target_host = (target_parts.hostname or "").lower()
            if target_parts.scheme not in {"http", "https"} or not _same_site(
                original_host, target_host
            ):
                raise CollectorHTTPError(
                    f"refused cross-site redirect for {self.spec.id}: {original_host} -> {target_host}"
                )
            if method == "POST" and response.status_code in {301, 302, 303}:
                raise CollectorHTTPError(
                    f"refused method-changing redirect for {self.spec.id}: {response.status_code}"
                )
            url = target
        raise CollectorHTTPError(f"too many redirects for {self.spec.id}")

    @staticmethod
    def next_cursor(response: httpx.Response, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        result = dict(previous or {})
        if response.headers.get("ETag"):
            result["etag"] = response.headers["ETag"]
        if response.headers.get("Last-Modified"):
            result["last_modified"] = response.headers["Last-Modified"]
        return result

    @abstractmethod
    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        raise NotImplementedError


MULTIPART_PUBLIC_SUFFIXES = {"co.uk", "com.cn", "com.hk", "com.au", "co.jp", "org.cn"}


def _registrable_domain(host: str) -> str:
    labels = host.strip(".").split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if last_two in MULTIPART_PUBLIC_SUFFIXES else last_two


def _same_site(left: str, right: str) -> bool:
    return bool(left and right and _registrable_domain(left) == _registrable_domain(right))
