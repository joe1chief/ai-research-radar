"""Boundary regressions: fake time and HTTP transports only, never production."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import select

from ai_research_radar.collectors.arxiv import ArxivCollector
from ai_research_radar.collectors.base import DomainRequestThrottle
from ai_research_radar.collectors.rss import RSSCollector
from ai_research_radar.contracts import SourceSpec, CollectionBatch, CollectedItem
from ai_research_radar.db import (
    SourceCursorModel,
    SourceHealthModel,
    ItemModel,
    sync_source,
    ingest_item,
)
from ai_research_radar.limits import Deadline, CollectionBudgetExceeded, retry_after_seconds
from ai_research_radar.pipeline import collect_group
from ai_research_radar.cli import configure_logging
from ai_research_radar.raw_storage import RawSnapshotStore


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def source(name="feed", **values):
    return SourceSpec(
        id=name,
        entity_id="test",
        group="tech",
        kind="rss",
        url=f"https://example.com/{name}?token=private-token",
        fetch_strategy="rss",
        parser="rss",
        evidence_type="official_company",
        **values,
    )


def test_retry_after_dates_and_invalid_values():
    now = datetime(2026, 10, 8, tzinfo=UTC)
    assert retry_after_seconds(format_datetime(now + timedelta(hours=2)), now=now) == 7200
    assert retry_after_seconds("7200") == 7200
    for value in ("invalid", "NaN", "inf", None):
        assert retry_after_seconds(value, now=now) is None


@pytest.mark.parametrize(
    "header", ["7200", format_datetime(datetime.now(UTC) + timedelta(hours=2))]
)
def test_long_retry_after_defers_without_sleep_or_second_request(header, caplog):
    configure_logging()
    calls, sleeps = [], []

    def reply(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": header}, request=request)

    clock = Clock()
    collector = RSSCollector(
        source(),
        client=httpx.Client(transport=httpx.MockTransport(reply)),
        deadline=Deadline(10, clock=clock),
        sleep=sleeps.append,
    )
    with caplog.at_level("INFO"), pytest.raises(CollectionBudgetExceeded) as caught:
        collector.collect()
    assert len(calls) == 1
    assert sleeps == []
    assert caught.value.retry_after_seconds > 7100
    assert "private-token" not in caplog.text
    assert "collector HTTP start" in caplog.text


def test_redirect_shares_budget_and_shrinks_request_timeout():
    clock, calls = Clock(), []

    def reply(request):
        calls.append(request.extensions["timeout"]["read"])
        clock.now += 2
        return httpx.Response(302, headers={"Location": "/next"}, request=request)

    collector = RSSCollector(
        source(),
        client=httpx.Client(transport=httpx.MockTransport(reply)),
        deadline=Deadline(3, clock=clock),
    )
    with pytest.raises(CollectionBudgetExceeded):
        collector.collect()
    assert calls == [3, 1]


def test_trickling_response_body_checks_wall_clock_and_closes_stream():
    clock = Clock()

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            for _ in range(5):
                clock.now += 1
                yield b"chunk"

        def close(self):
            self.closed = True

    body = Body()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=body, request=r))
    )
    with pytest.raises(CollectionBudgetExceeded):
        RSSCollector(source(), client=client, deadline=Deadline(2, clock=clock)).collect()
    assert body.closed
    assert clock.now == 2


def test_pagination_interval_and_next_page_share_budget():
    clock, calls = Clock(), []
    feed = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2601.00001v1</id><title>Agent</title></entry></feed>'

    def reply(request):
        calls.append(request)
        clock.now += 1
        return httpx.Response(200, text=feed, request=request)

    spec = source().model_copy(
        update={"kind": "arxiv_api", "page_size": 1, "max_pages": 8, "request_interval_seconds": 3}
    )
    collector = ArxivCollector(
        spec,
        client=httpx.Client(transport=httpx.MockTransport(reply)),
        deadline=Deadline(3, clock=clock),
        sleep=clock.sleep,
    )
    with pytest.raises(CollectionBudgetExceeded):
        collector.collect()
    assert len(calls) == 1
    assert clock.now == 1  # no unnecessary sleep after the budget cannot fit it


def test_domain_throttle_cannot_outwait_active_budget():
    clock = Clock()
    throttle = DomainRequestThrottle(3, clock=clock, sleep=clock.sleep)
    with Deadline(2, clock=clock).activate():
        throttle.wait("https://data.sec.gov/first")
        with pytest.raises(CollectionBudgetExceeded):
            throttle.wait("https://www.sec.gov/second")
    assert clock.now == 0


def test_retry_not_before_survives_failure_and_force_cannot_bypass(session):
    bad, good = source("bad"), source("good", allow_empty=True)
    old = SourceCursorModel(source_id=bad.id, cursor={"watermark": "old"}, etag='"old"')
    sync_source(session, bad)
    session.add(old)
    session.commit()
    calls = []

    def reply(request):
        calls.append(request.url.path)
        if request.url.path == "/bad":
            return httpx.Response(429, headers={"Retry-After": "7200"}, request=request)
        return httpx.Response(200, text="<rss><channel/></rss>", request=request)

    client = httpx.Client(transport=httpx.MockTransport(reply))
    stats = collect_group(
        session, [bad, good], group="tech", user_agent="test", shared_client=client
    )
    assert (stats.failed, stats.sources) == (1, 2)
    assert calls == ["/bad", "/good"]
    assert session.get(SourceCursorModel, bad.id).cursor == {"watermark": "old"}
    assert session.get(SourceCursorModel, bad.id).etag == '"old"'
    assert session.get(SourceHealthModel, good.id).status == "healthy"
    health = session.get(SourceHealthModel, bad.id)
    assert datetime.fromisoformat(health.metadata_json["retry_not_before"]) > datetime.now(
        UTC
    ) + timedelta(minutes=119)
    collect_group(session, [bad], group="tech", user_agent="test", shared_client=client, force=True)
    assert calls == ["/bad", "/good"]


def test_ingest_budget_rolls_back_items_and_cursor_then_continues(session, monkeypatch, caplog):
    clock, bad, good = Clock(), source("slow"), source("healthy", allow_empty=True)
    sync_source(session, bad)
    session.add(SourceCursorModel(source_id=bad.id, cursor={"position": "old"}))
    session.commit()

    class Collector:
        def __init__(self, spec):
            self.spec = spec

        def collect(self, cursor):
            return CollectionBatch(
                items=[
                    CollectedItem(
                        source_id=self.spec.id,
                        external_id="one",
                        canonical_url="https://example.com/one",
                        title="Agent",
                    )
                ],
                cursor={"position": "new"},
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for", lambda spec, **_: Collector(spec)
    )

    def slow_ingest(active, spec, item):
        result = ingest_item(active, spec, item)
        if spec.id == bad.id:
            clock.now += 3
        return result

    monkeypatch.setattr("ai_research_radar.pipeline.ingest_item", slow_ingest)
    with caplog.at_level("INFO"):
        stats = collect_group(
            session,
            [bad, good],
            group="tech",
            user_agent="test",
            source_budget_seconds=2,
            group_budget_seconds=10,
            clock=clock,
        )
    assert stats.failed == 1 and stats.changed == 1
    assert session.get(SourceCursorModel, bad.id).cursor == {"position": "old"}
    assert [item.source_id for item in session.scalars(select(ItemModel))] == [good.id]
    assert session.get(SourceHealthModel, good.id).status == "healthy"
    assert "stage=persist" in caplog.text and "stage=commit" in caplog.text


def test_group_budget_stops_remaining_sources_without_advancing_cursor(session, monkeypatch):
    clock, seen = Clock(), []

    class Collector:
        def collect(self, cursor):
            clock.now += 3
            return CollectionBatch(cursor={"new": True})

        def close(self):
            pass

    def build(spec, **kwargs):
        seen.append(spec.id)
        return Collector()

    monkeypatch.setattr("ai_research_radar.pipeline.collector_for", build)
    stats = collect_group(
        session,
        [source("first"), source("later")],
        group="tech",
        user_agent="test",
        group_budget_seconds=2,
        source_budget_seconds=10,
        clock=clock,
    )
    assert stats.budget_exhausted == 1 and stats.failed == 1
    assert seen == ["first"]
    assert session.get(SourceCursorModel, "first").cursor == {}


def test_swallowed_terminal_rate_limit_still_blocks_source_cursor(session, monkeypatch):
    spec = source()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "1"}, request=r)
        )
    )

    class CatchingCollector(RSSCollector):
        def collect(self, cursor):
            try:
                self.request()
            except Exception:
                return CollectionBatch(cursor={"incorrect": "advanced"}, warnings=["venue failed"])

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for",
        lambda spec, **kwargs: CatchingCollector(spec, max_attempts=1, **kwargs),
    )
    stats = collect_group(session, [spec], group="tech", user_agent="test", shared_client=client)
    assert stats.failed == 1
    assert session.get(SourceCursorModel, spec.id).cursor == {}


def test_raw_upload_budget_failure_rolls_back_source(session, monkeypatch):
    clock, spec = Clock(), source()

    class Collector:
        def collect(self, cursor):
            return CollectionBatch(
                items=[
                    CollectedItem(
                        source_id=spec.id,
                        external_id="one",
                        canonical_url="https://example.com/one",
                        title="Agent",
                        raw_snapshot=b"private",
                    )
                ],
                cursor={"position": "new"},
            )

        def close(self):
            pass

    class Store:
        def put(self, *, deadline, **kwargs):
            clock.now += 3
            deadline.remaining("raw_upload_response")

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for", lambda *_args, **_kwargs: Collector()
    )
    stats = collect_group(
        session,
        [spec],
        group="tech",
        user_agent="test",
        raw_store=Store(),
        source_budget_seconds=2,
        clock=clock,
    )
    assert stats.failed == 1
    assert session.get(SourceCursorModel, spec.id).cursor == {}
    assert session.scalar(select(ItemModel)) is None


def test_raw_storage_stream_uses_remaining_budget_and_closes_on_expiry():
    clock, timeouts = Clock(), []

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            clock.now += 3
            yield b"{}"

        def close(self):
            self.closed = True

    body = Body()

    def reply(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, stream=body, request=request)

    store = RawSnapshotStore(
        supabase_url="https://example.invalid",
        secret_key="unused",
        client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    with pytest.raises(CollectionBudgetExceeded):
        store.put(
            source_id="test",
            item_id="test",
            content_hash="hash",
            payload=b"data",
            fetched_at=datetime.now(UTC),
            deadline=Deadline(2, clock=clock),
        )
    assert timeouts == [2] and body.closed


def test_cleanup_error_does_not_turn_committed_cursor_into_failed_source(session, monkeypatch):
    spec = source(allow_empty=True)

    class Collector:
        def collect(self, cursor):
            return CollectionBatch(cursor={"position": "new"})

        def close(self):
            raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(
        "ai_research_radar.pipeline.collector_for", lambda *_args, **_kwargs: Collector()
    )
    stats = collect_group(session, [spec], group="tech", user_agent="test")
    assert stats.failed == 0
    assert session.get(SourceCursorModel, spec.id).cursor == {"position": "new"}
    assert session.get(SourceHealthModel, spec.id).status == "healthy"


def test_transport_retry_budget_error_does_not_retain_sensitive_context():
    clock = Clock()

    def reply(request):
        clock.now += 2
        raise httpx.ReadTimeout("private-token", request=request)

    collector = RSSCollector(
        source(),
        client=httpx.Client(transport=httpx.MockTransport(reply)),
        deadline=Deadline(2, clock=clock),
    )
    with pytest.raises(CollectionBudgetExceeded) as caught:
        collector.collect()
    assert caught.value.__context__ is None
    assert "private-token" not in str(caught.value)
