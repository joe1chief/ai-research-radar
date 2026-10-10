from datetime import UTC, datetime, timedelta

import pytest

from ai_research_radar.contracts import CollectionBatch, SourceSpec
from ai_research_radar.db import SourceHealthModel, SourceModel, sync_source
from ai_research_radar.pipeline import collect_group
from ai_research_radar.limits import CollectionBudgetExceeded


@pytest.mark.parametrize(
    "error", [CollectionBudgetExceeded("source_order"), RuntimeError("private SQL")]
)
def test_order_failure_reports_without_fetching_or_leaking(session, monkeypatch, caplog, error):
    from ai_research_radar import pipeline

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(pipeline, "_tech_source_order", fail)
    monkeypatch.setattr(pipeline, "collector_for", lambda *a, **kw: pytest.fail("no HTTP"))
    stats = collect_group(session, [source("ready")], group="tech", user_agent="test")
    assert stats.failed == 1 and stats.sources == 0
    assert stats.budget_exhausted == int(isinstance(error, CollectionBudgetExceeded))
    assert "private SQL" not in caplog.text
    assert not session.in_transaction()


def test_order_query_elapsed_budget_stops_before_fetch(session, monkeypatch):
    from ai_research_radar import pipeline

    clock = [0.0]

    def order(session, specs, **kwargs):
        clock[0] = 3.0
        return specs

    monkeypatch.setattr(pipeline, "_tech_source_order", order)
    monkeypatch.setattr(pipeline, "collector_for", lambda *a, **kw: pytest.fail("no HTTP"))
    stats = collect_group(
        session,
        [source("ready")],
        group="tech",
        user_agent="test",
        clock=lambda: clock[0],
        group_budget_seconds=2,
    )
    assert stats.budget_exhausted == 1 and stats.failed == 1 and stats.sources == 0


def source(name, **kwargs):
    return SourceSpec(
        id=name,
        entity_id="test",
        group="tech",
        kind="rss",
        url=f"https://example.invalid/{name}",
        fetch_strategy="rss",
        parser="rss",
        evidence_type="official_company",
        cadence="four_hour",
        allow_empty=True,
        **kwargs,
    )


def test_persisted_attempts_rotate_across_runs_within_existing_budget(session, monkeypatch):
    from ai_research_radar import pipeline

    specs = [source(n) for n in "abcde"]
    seen, completed = [], []
    clock = [0.0]
    base = [datetime(2026, 10, 10, tzinfo=UTC)]
    monkeypatch.setattr(pipeline, "utcnow", lambda: base[0] + timedelta(seconds=clock[0]))

    class Collector:
        def __init__(self, spec, deadline):
            self.spec, self.deadline = spec, deadline

        def collect(self, cursor):
            seen.append(self.spec.id)
            clock[0] += min(1, self.deadline.remaining("mock_request"))
            self.deadline.remaining("mock_response")
            completed.append(self.spec.id)
            return CollectionBatch()

        def close(self):
            pass

    monkeypatch.setattr(
        pipeline, "collector_for", lambda spec, **kw: Collector(spec, kw["deadline"])
    )
    for _ in range(5):
        clock[0] = 0
        stats = collect_group(
            session,
            specs,
            group="tech",
            user_agent="test",
            clock=lambda: clock[0],
            group_budget_seconds=2.5,
            source_budget_seconds=2,
        )
        assert clock[0] == 2.5
        assert stats.budget_exhausted == 1 and stats.failed == 1
        base[0] += timedelta(hours=5)
    assert seen[:6] == ["a", "b", "c", "d", "e", "a"]
    assert set(completed) == set("abcde")
    assert all(seen.count(n) >= 2 for n in "abcde")
    assert all(session.get(SourceHealthModel, n).last_attempt_at for n in "abcde")


def test_fair_order_preserves_force_cooldown_and_disabled_guards(session, monkeypatch):
    from ai_research_radar import pipeline

    now = datetime.now(UTC)
    ready, not_due, paused, disabled = [
        source(n, enabled=n != "disabled") for n in ["ready", "not_due", "paused", "disabled"]
    ]
    for spec in [not_due, paused, disabled, ready]:
        sync_source(session, spec)
        session.add(SourceHealthModel(source_id=spec.id, last_attempt_at=now - timedelta(days=10)))
    session.flush()
    session.get(SourceModel, "not_due").next_due_at = now + timedelta(days=1)
    session.get(SourceHealthModel, "paused").metadata_json = {
        "retry_not_before": (now + timedelta(days=1)).isoformat()
    }
    session.commit()
    seen = []

    class Collector:
        def __init__(self, spec):
            self.spec = spec

        def collect(self, cursor):
            seen.append(self.spec.id)
            return CollectionBatch()

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "collector_for", lambda spec, **_: Collector(spec))
    specs = [disabled, paused, not_due, ready]
    collect_group(session, specs, group="tech", user_agent="test")
    assert seen == ["ready"]
    collect_group(session, specs, group="tech", user_agent="test", force=True)
    assert set(seen) == {"ready", "not_due"}
    assert "paused" not in seen and "disabled" not in seen


def test_nontech_config_order_unchanged(session, monkeypatch):
    from ai_research_radar import pipeline

    seen = []

    class Collector:
        def __init__(self, spec):
            self.spec = spec

        def collect(self, cursor):
            seen.append(self.spec.id)
            return CollectionBatch()

        def close(self):
            pass

    specs = [source(n).model_copy(update={"group": "standards"}) for n in "ba"]
    monkeypatch.setattr(pipeline, "collector_for", lambda spec, **_: Collector(spec))
    collect_group(session, specs, group="standards", user_agent="test")
    assert seen == ["b", "a"]
