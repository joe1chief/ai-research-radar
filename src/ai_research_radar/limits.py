"""Cooperative wall-clock budgets shared by HTTP collection and persistence."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import httpx


class CollectionBudgetExceeded(RuntimeError):
    def __init__(self, stage: str, *, retry_after_seconds: float | None = None):
        super().__init__(f"collection budget exhausted: stage={stage}")
        self.retry_after_seconds = retry_after_seconds


ACTIVE_DEADLINE: ContextVar[Deadline | None] = ContextVar("radar_deadline", default=None)


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("budget must be finite and positive")
        self.clock = clock
        self.ends_at = clock() + seconds
        self.failure: CollectionBudgetExceeded | None = None

    def remaining(self, stage: str) -> float:
        if self.failure is not None:
            raise self.failure
        remaining = self.ends_at - self.clock()
        if remaining <= 0:
            self.failure = CollectionBudgetExceeded(stage)
            raise self.failure
        return remaining

    def sleep(
        self,
        seconds: float,
        *,
        sleep: Callable[[float], None],
        stage: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        if seconds >= self.remaining(stage):
            self.failure = CollectionBudgetExceeded(stage, retry_after_seconds=retry_after_seconds)
            raise self.failure
        sleep(seconds)
        self.remaining(stage)

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = ACTIVE_DEADLINE.set(self)
        try:
            yield
        finally:
            ACTIVE_DEADLINE.reset(token)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Accept delta seconds and HTTP dates; reject NaN, infinity and invalid dates."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            seconds = (target - (now or datetime.now(UTC))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def read_response(response: httpx.Response, deadline: Deadline, stage: str) -> httpx.Response:
    """Buffer raw chunks with budget checks, then let HTTPX decode normally."""
    deadline.remaining(stage)
    if response.is_stream_consumed:
        return response  # MockTransport may supply an already-buffered response.
    chunks = []
    for chunk in response.iter_raw():
        deadline.remaining(stage)
        chunks.append(chunk)
    result = httpx.Response(
        response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=response.request,
        extensions=response.extensions,
    )
    deadline.remaining(stage)
    return result
