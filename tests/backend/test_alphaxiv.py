from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from ai_research_radar.alphaxiv import (
    AlphaXivInsight,
    MCPAlphaXivAdapter,
    enrich_alphaxiv_top,
)
from ai_research_radar.db import (
    EventItemModel,
    EventRevisionModel,
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    SourceModel,
)


def test_mcp_alphaxiv_initializes_session_and_reads_structured_report():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": "session-1"},
                request=request,
            )
        if payload["method"] == "notifications/initialized":
            assert request.headers["Mcp-Session-Id"] == "session-1"
            return httpx.Response(202, request=request)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Main contribution: persistent agent memory.\n- Evaluated on long tasks.",
                        }
                    ]
                },
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = MCPAlphaXivAdapter(
        access_token="oauth-token",
        endpoint="https://api.alphaxiv.example/mcp/v1",
        client=client,
    )
    insight = adapter.deep_read("2607.00001")
    assert insight is not None
    assert "persistent agent memory" in insight.summary
    assert insight.key_findings == ["Evaluated on long tasks."]
    assert [call["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert calls[-1]["params"]["name"] == "get_paper_content"


class FakeAlphaXiv:
    def deep_read(self, arxiv_id: str) -> AlphaXivInsight:
        return AlphaXivInsight(arxiv_id, "structured report", ["finding"])


def test_top_paper_enrichment_is_bounded_and_idempotent(session):
    now = datetime.now(UTC)
    source = SourceModel(
        id="arxiv",
        entity_id="arxiv",
        group="papers",
        kind="arxiv_api",
        url="https://export.arxiv.org/api/query",
        fetch_strategy="arxiv_query",
        cadence="daily",
        evidence_type="paper",
        parser="atom",
        enabled=True,
    )
    item = ItemModel(
        id="item-1",
        source_id="arxiv",
        native_id="2607.00001",
        canonical_url="https://arxiv.org/abs/2607.00001v1",
        item_type="arxiv_api",
        title="Long Horizon Agent",
        current_content_hash="a" * 64,
        metadata_json={},
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    version = ItemVersionModel(
        id="version-1",
        item_id="item-1",
        version_key="1",
        content_hash="a" * 64,
        title=item.title,
        metadata_json={"arxiv_id": "2607.00001"},
        fetched_at=now,
    )
    event = RadarEventModel(
        id="event-1",
        cluster_id="event-1",
        event_type="PAPER",
        topics=["long_horizon"],
        entities=[],
        cross_tags=[],
        title_zh=item.title,
        summary_zh="summary",
        why_it_matters="why",
        status="NEW_ENTITY",
        source_type="arxiv_api",
        verification_status="verified_primary",
        score=90,
        primary_url=item.canonical_url,
        corroborating_urls=[],
        is_public=True,
        first_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(source)
    session.flush()
    session.add_all([item, event])
    session.flush()
    session.add(version)
    session.flush()
    revision = EventRevisionModel(
        id="revision-1",
        event_id=event.id,
        revision_no=1,
        content_hash="a" * 64,
        status="NEW_ENTITY",
        is_material=True,
        snapshot={},
        created_at=now,
    )
    session.add(revision)
    session.add(EventItemModel(event_id=event.id, item_version_id=version.id, relation="primary"))
    session.flush()

    first = enrich_alphaxiv_top(session, FakeAlphaXiv(), limit=1)
    second = enrich_alphaxiv_top(session, FakeAlphaXiv(), limit=1)
    assert first == {"attempted": 1, "enriched": 1, "failed": 0}
    assert second == {"attempted": 0, "enriched": 0, "failed": 0}
    assert version.metadata_json["alphaxiv_insight"]["summary"] == "structured report"
    assert revision.snapshot["alphaxiv_insight"]["key_findings"] == ["finding"]
