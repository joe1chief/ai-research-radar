from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx

from ai_research_radar.collectors import CNInfoAnnouncementsCollector, collector_for
from ai_research_radar.collectors.cninfo import _date_window
from ai_research_radar.config import load_sources
from ai_research_radar.contracts import SourceSpec


def _spec(**overrides) -> SourceSpec:
    values = {
        "id": "cninfo-iflytek-test",
        "entity_id": "iflytek",
        "group": "capital",
        "kind": "cninfo_announcements",
        "url": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        "fetch_strategy": "cninfo_official_api",
        "cadence": "four_hour",
        "evidence_type": "exchange_filing",
        "parser": "cninfo_json",
        "stock": "002230,9900004565",
        "ticker": "002230",
        "org_id": "9900004565",
        "page_size": 2,
        "max_pages": 2,
        "lookback_days": 45,
        "overlap_days": 7,
        "routine_title_patterns": ["权益分派|股东会"],
    }
    values.update(overrides)
    return SourceSpec.model_validate(values)


def test_cninfo_posts_official_query_and_normalizes_paginated_announcements():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = parse_qs(request.content.decode())
        page = int(form["pageNum"][0])
        assert form["stock"] == ["002230,9900004565"]
        assert form["column"] == ["szse"]
        assert "~" in form["seDate"][0]
        common = {
            "announcementTime": 1783353600000,
            "secCode": "002230",
            "secName": "科大讯飞",
            "orgId": "9900004565",
        }
        payload = {
            "announcements": [
                {
                    **common,
                    "announcementId": "1225410796",
                    "announcementTitle": "2025年年度<em>权益分派</em>实施公告",
                    "adjunctUrl": "finalpage/2026-07-07/1225410796.PDF",
                },
                {
                    **common,
                    "announcementId": "1225400000",
                    "announcementTitle": "重大合同公告",
                    "announcementTime": 1783267200000,
                    "adjunctUrl": "finalpage/2026-07-06/1225400000.PDF",
                },
            ]
            if page == 1
            else [
                {
                    **common,
                    "announcementId": "1225400000",
                    "announcementTitle": "重大合同公告",
                    "announcementTime": 1783267200000,
                    "adjunctUrl": "finalpage/2026-07-06/1225400000.PDF",
                }
            ],
            "hasMore": page == 1,
        }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    batch = CNInfoAnnouncementsCollector(_spec(), client=client).collect()

    assert len(requests) == 2
    assert all(request.method == "POST" for request in requests)
    assert requests[0].headers["Origin"] == "https://www.cninfo.com.cn"
    assert requests[0].headers["X-Requested-With"] == "XMLHttpRequest"
    assert [item.external_id for item in batch.items] == ["1225410796", "1225400000"]
    first = batch.items[0]
    assert first.title == "科大讯飞 · 2025年年度权益分派实施公告"
    assert first.canonical_url == (
        "https://static.cninfo.com.cn/finalpage/2026-07-07/1225410796.PDF"
    )
    assert first.published_at == datetime(2026, 7, 6, 16, tzinfo=UTC)
    assert first.updated_at is None
    assert first.evidence_type == "exchange_filing"
    assert first.metadata["document_id"] == "1225410796"
    assert first.metadata["ticker"] == "002230"
    assert first.metadata["exchange"] == "SZSE"
    assert first.metadata["disclosure_date"] == "2026-07-07"
    assert first.metadata["routine"] is True
    assert batch.items[1].metadata["routine"] is False
    assert batch.cursor["last_seen_native_id"] == "1225410796"


def test_cninfo_overlap_window_is_bounded_and_malformed_urls_are_skipped():
    start, end = _date_window(
        {"last_seen_at": "2026-07-10T00:00:00+08:00"},
        _spec(),
        now=datetime(2026, 7, 12, 12, tzinfo=UTC),
    )
    assert start.isoformat() == "2026-07-03"
    assert end.isoformat() == "2026-07-12"

    payload = {
        "announcements": [
            {
                "announcementId": "unsafe",
                "announcementTitle": "公告",
                "announcementTime": 1783353600000,
                "adjunctUrl": "https://evil.example/filing.pdf",
            }
        ],
        "hasMore": False,
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    batch = CNInfoAnnouncementsCollector(_spec(), client=client).collect()
    assert batch.items == []
    assert batch.warnings == ["CNInfo skipped malformed announcement unsafe"]


def test_cninfo_registry_and_checked_in_source_config():
    assert isinstance(collector_for(_spec()), CNInfoAnnouncementsCollector)
    source = {source.id: source for source in load_sources("configs")}["cninfo-iflytek"]
    assert source.enabled is True
    assert source.entity_id == "iflytek"
    assert source.evidence_type == "exchange_filing"
    assert source.stock == "002230,9900004565"
    assert source.allow_empty is True
