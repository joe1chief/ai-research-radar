from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ai_research_radar.collectors import SAMRStandardsCollector, collector_for
from ai_research_radar.config import load_sources
from ai_research_radar.contracts import SourceSpec


FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "samr_standards_pages.json").read_text(
        encoding="utf-8"
    )
)


def _spec(**overrides) -> SourceSpec:
    values = {
        "id": "samr-test",
        "entity_id": "samr",
        "group": "standards",
        "kind": "samr_standards",
        "url": "https://std.samr.gov.cn/gb/search/gbQueryPage",
        "fetch_strategy": "samr_official_json_search",
        "cadence": "daily",
        "evidence_type": "official_standard",
        "parser": "samr_json",
        "search_terms": ["人工智能", "大模型"],
        "page_size": 2,
        "max_pages_per_term": 2,
    }
    values.update(overrides)
    return SourceSpec.model_validate(values)


def test_samr_queries_terms_paginates_normalizes_and_deduplicates_fixture():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        term = request.url.params["searchText"]
        page = request.url.params["pageNumber"]
        return httpx.Response(200, json=FIXTURE[f"{term}:{page}"], request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    batch = SAMRStandardsCollector(_spec(), client=client).collect()

    requested_pages = [
        (request.url.params["searchText"], request.url.params["pageNumber"])
        for request in requests
    ]
    assert requested_pages == [
        ("人工智能", "1"),
        ("人工智能", "2"),
        ("大模型", "1"),
    ]
    assert all(request.method == "GET" for request in requests)
    assert requests[0].headers["Referer"] == "https://std.samr.gov.cn/gb/gbQuery"
    assert requests[0].headers["X-Requested-With"] == "XMLHttpRequest"
    assert len(batch.items) == 4

    by_id = {item.external_id: item for item in batch.items}
    first = by_id["2FF37940EB7FD753E06397BE0A0A413F"]
    assert first.title == (
        "GB/T 45280-2025 · 人工智能 异构人工智能加速器统一接口"
    )
    assert first.canonical_url == (
        "https://std.samr.gov.cn/gb/search/gbDetailed"
        "?id=2FF37940EB7FD753E06397BE0A0A413F"
    )
    assert first.published_at == datetime(2025, 2, 28, tzinfo=UTC)
    assert first.updated_at is None
    assert first.evidence_type == "official_standard"
    assert first.metadata["standard_code"] == "GB/T 45280-2025"
    assert first.metadata["state"] == "现行"
    assert first.metadata["project_id"] == 1007208
    assert "性质：推荐性" in first.summary

    duplicate = by_id["511EBC5967DA9318E06397BE0A0AFBD5"]
    assert duplicate.metadata["matched_search_terms"] == ["人工智能", "大模型"]
    assert batch.cursor["search_totals"] == {"人工智能": 3, "大模型": 2}
    assert batch.cursor["last_seen_at"] == "2026-07-01T00:00:00+00:00"


def test_samr_skips_malformed_rows_and_warns_when_page_budget_is_reached():
    payload = {
        "total": 5,
        "rows": [
            {"id": "bad", "C_C_NAME": "unsafe identifier"},
            {
                "id": "B2345678901234567890123456789012",
                "C_C_NAME": "<b>人工智能安全治理</b>",
                "C_STD_CODE": "GB/T 10000-2026",
                "ISSUE_DATE": "not-a-date",
            },
        ],
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    batch = SAMRStandardsCollector(
        _spec(search_terms=["人工智能"], max_pages_per_term=1), client=client
    ).collect()

    assert len(batch.items) == 1
    assert batch.items[0].published_at is None
    assert any("skipped 1 malformed rows" in warning for warning in batch.warnings)
    assert any("page budget reached" in warning for warning in batch.warnings)


def test_samr_registry_and_checked_in_source_config():
    collector = collector_for(_spec())
    try:
        assert isinstance(collector, SAMRStandardsCollector)
    finally:
        collector.close()

    source = {source.id: source for source in load_sources("configs")}["samr-ai-standards"]
    assert source.enabled is True
    assert source.kind == "samr_standards"
    assert source.url == "https://std.samr.gov.cn/gb/search/gbQueryPage"
    assert source.search_terms == ["人工智能", "大模型"]
    assert source.evidence_type == "official_standard"
