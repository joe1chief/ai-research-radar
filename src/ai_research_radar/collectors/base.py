"""HTTP behavior shared by source collectors."""

from __future__ import annotations

import random
import threading
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
    """A redacted HTTP failure safe to log and persist.

    Request URLs can contain sensitive query parameters and provider response
    bodies can echo credentials. Keep only the source-controlled message and
    explicitly safe diagnostic fields on this boundary exception.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        host: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.host = host


class DomainRequestThrottle:
    """Reserve request slots per registrable domain across collector instances."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be non-negative")
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        domain = _registrable_domain(host)
        if not domain or self.min_interval_seconds == 0:
            return
        with self._lock:
            now = self.clock()
            ready_at = max(now, self._next_allowed.get(domain, now))
            self._next_allowed[domain] = ready_at + self.min_interval_seconds
        delay = ready_at - now
        if delay > 0:
            self.sleep(delay)


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
        request_throttle: Callable[[str], None] | None = None,
    ) -> None:
        self.spec = spec
        self._owns_client = client is None
        self._default_headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, application/atom+xml, application/rss+xml, text/html;q=0.9",
        }
        if authorization:
            self._default_headers["Authorization"] = authorization
        self.client = client or httpx.Client(
            timeout=spec.timeout_seconds,
            follow_redirects=True,
            headers=self._default_headers,
        )
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.request_throttle = request_throttle
        self.last_http_status: int | None = None

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
        # Apply the collector-scoped identity even when a caller supplies a
        # shared httpx client with different defaults.
        headers = dict(self._default_headers)
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

        for attempt in range(1, self.max_attempts + 1):
            transport_failure: tuple[str, str | None] | None = None
            try:
                response = self._request_with_safe_redirects(
                    method=method,
                    params=params,
                    data=data,
                    headers=headers,
                    url=url or self.spec.url,
                )
            except httpx.TransportError as exc:
                self.last_http_status = None
                if attempt < self.max_attempts:
                    self.sleep(2 ** (attempt - 1) + random.uniform(0, 0.25))
                    continue
                transport_failure = (
                    type(exc).__name__,
                    _safe_host(url or self.spec.url),
                )

            # Raise only after leaving the transport exception handler. This
            # prevents the redacted boundary exception from retaining the
            # original request URL or transport message via cause/context.
            if transport_failure is not None:
                error_type, host = transport_failure
                raise CollectorHTTPError(
                    _safe_failure_message(
                        self.spec.id,
                        host=host,
                        status_code=None,
                        retryable=True,
                        error_type=error_type,
                    ),
                    retryable=True,
                    host=host,
                )

            self.last_http_status = response.status_code
            if response.status_code == 304:
                return response
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self.max_attempts:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 2 ** (attempt - 1)
                )
                self.sleep(delay + random.uniform(0, 0.25))
                continue
            if not response.is_success:
                host = (response.request.url.host or "").lower() or None
                status_code = response.status_code
                raise CollectorHTTPError(
                    _safe_failure_message(
                        self.spec.id,
                        host=host,
                        status_code=status_code,
                        retryable=retryable,
                        error_type="HTTPStatusError",
                    ),
                    status_code=status_code,
                    retryable=retryable,
                    host=host,
                )
            return response
        raise AssertionError("collector retry loop exhausted without a result")

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
        last_redirect_status: int | None = None
        for redirect_count in range(6):
            if self.request_throttle is not None:
                self.request_throttle(url)
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
            last_redirect_status = response.status_code
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
                    f"refused cross-site redirect for {self.spec.id}",
                    status_code=response.status_code,
                    retryable=False,
                    host=target_host or None,
                )
            if method == "POST" and response.status_code in {301, 302, 303}:
                raise CollectorHTTPError(
                    f"refused method-changing redirect for {self.spec.id}",
                    status_code=response.status_code,
                    retryable=False,
                    host=target_host or original_host or None,
                )
            url = target
        raise CollectorHTTPError(
            f"too many redirects for {self.spec.id}",
            status_code=last_redirect_status,
            retryable=False,
            host=original_host or None,
        )

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


def _safe_host(url: str) -> str | None:
    return (urlsplit(url).hostname or "").lower() or None


def _safe_failure_message(
    source_id: str,
    *,
    host: str | None,
    status_code: int | None,
    retryable: bool,
    error_type: str,
) -> str:
    return (
        f"failed to fetch {source_id}: host={host or 'unknown'} "
        f"status_code={status_code if status_code is not None else 'none'} "
        f"retryable={str(retryable).lower()} error_type={error_type}"
    )
