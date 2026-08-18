from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import httpx

from ai_research_radar.collectors.sec import SECSubmissionsCollector
from ai_research_radar.contracts import SourceSpec
from ai_research_radar.pipeline import _cursor_payload


NOW = datetime.fromisoformat("2026-07-12T13:45:00+00:00")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0001652044.json"


def _spec(**updates) -> SourceSpec:
    values = {
        "id": "sec-alphabet",
        "entity_id": "alphabet",
        "group": "capital",
        "kind": "sec_submissions",
        "url": SUBMISSIONS_URL,
        "fetch_strategy": "sec_submissions",
        "cadence": "four_hour",
        "evidence_type": "regulatory_filing",
        "cursor_strategy": "accession_number",
        "parser": "sec_json",
        "lookback_days": 3,
        "filing_detail_limit": 1,
    }
    values.update(updates)
    return SourceSpec.model_validate(values)


def _payload(rows: list[dict[str, str]]) -> dict:
    keys = {
        "accessionNumber",
        "form",
        "filingDate",
        "acceptanceDateTime",
        "primaryDocument",
        "primaryDocDescription",
        "items",
    }
    return {
        "cik": "1652044",
        "name": "Alphabet Inc.",
        "filings": {
            "recent": {
                key: [row.get(key, "") for row in rows]
                for key in keys
            }
        },
    }


def _client(payload: dict, detail_requests: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json=payload, request=request)
        if detail_requests is not None:
            detail_requests.append(str(request.url))
        return httpx.Response(
            200,
            text="<html><body><p>Material filing detail.</p></body></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_first_run_uses_lookback_window_and_keeps_newest_first_detail_budget():
    rows = [
        {
            "accessionNumber": "0001-26-000001",
            "form": "DEF 14A",
            "filingDate": "2026-07-12",
            "acceptanceDateTime": "2026-07-12T15:00:00Z",
            "primaryDocument": "proxy.htm",
        },
        {
            "accessionNumber": "0001-26-000002",
            "form": "8-K",
            "filingDate": "2026-07-11",
            "acceptanceDateTime": "2026-07-11T13:00:00Z",
            "primaryDocument": "new.htm",
            "primaryDocDescription": "Current report",
        },
        {
            "accessionNumber": "0001-26-000003",
            "form": "6-K",
            "filingDate": "2026-07-10",
            "acceptanceDateTime": "2026-07-10T12:00:00Z",
            "primaryDocument": "boundary.htm",
        },
        {
            "accessionNumber": "0001-26-000004",
            "form": "8-K",
            "filingDate": "2026-07-09",
            "acceptanceDateTime": "2026-07-09T12:00:00Z",
            "primaryDocument": "too-old.htm",
        },
    ]
    detail_requests: list[str] = []
    collector = SECSubmissionsCollector(
        _spec(),
        client=_client(_payload(rows), detail_requests),
        now=lambda: NOW,
    )

    batch = collector.collect()

    assert [item.external_id for item in batch.items] == [
        "0001-26-000002",
        "0001-26-000003",
    ]
    assert batch.cursor["last_seen_native_id"] == "0001-26-000001"
    assert len(detail_requests) == 1
    assert detail_requests[0].endswith("/000126000002/new.htm")
    assert batch.items[0].content == "Material filing detail."
    assert batch.items[1].content == ""
    assert batch.warnings == []


def test_incremental_run_stops_at_last_seen_accession_even_inside_lookback():
    rows = [
        {
            "accessionNumber": "0001-26-000010",
            "form": "8-K",
            "filingDate": "2026-07-12",
            "acceptanceDateTime": "2026-07-12T14:00:00Z",
            "primaryDocument": "new.htm",
        },
        {
            "accessionNumber": "0001-26-000009",
            "form": "DEF 14A",
            "filingDate": "2026-07-12",
            "acceptanceDateTime": "2026-07-12T13:00:00Z",
            "primaryDocument": "proxy.htm",
        },
        {
            "accessionNumber": "0001-26-000008",
            "form": "8-K",
            "filingDate": "2026-07-11",
            "acceptanceDateTime": "2026-07-11T12:00:00Z",
            "primaryDocument": "watermark.htm",
        },
        {
            "accessionNumber": "0001-26-000007",
            "form": "8-K",
            "filingDate": "2026-07-11",
            "acceptanceDateTime": "2026-07-11T11:00:00Z",
            "primaryDocument": "must-not-replay.htm",
        },
    ]
    collector = SECSubmissionsCollector(
        _spec(filing_detail_limit=0),
        client=_client(_payload(rows)),
        now=lambda: NOW,
    )

    batch = collector.collect({"last_seen_native_id": "0001-26-000008"})

    assert [item.external_id for item in batch.items] == ["0001-26-000010"]
    assert batch.cursor["last_seen_native_id"] == "0001-26-000010"
    assert batch.warnings == []


def test_missing_sec_cursor_falls_back_to_bounded_window_with_warning():
    rows = [
        {
            "accessionNumber": "0001-26-000010",
            "form": "8-K",
            "filingDate": "2026-07-12",
            "acceptanceDateTime": "2026-07-12T14:00:00Z",
            "primaryDocument": "new.htm",
        },
        {
            "accessionNumber": "0001-26-000009",
            "form": "8-K",
            "filingDate": "2026-07-10",
            "acceptanceDateTime": "2026-07-10T14:00:00Z",
            "primaryDocument": "boundary.htm",
        },
        {
            "accessionNumber": "0001-26-000008",
            "form": "8-K",
            "filingDate": "2026-07-09",
            "acceptanceDateTime": "2026-07-09T14:00:00Z",
            "primaryDocument": "old.htm",
        },
    ]
    collector = SECSubmissionsCollector(
        _spec(filing_detail_limit=0),
        client=_client(_payload(rows)),
        now=lambda: NOW,
    )

    batch = collector.collect({"last_seen_native_id": "expired-accession"})

    assert [item.external_id for item in batch.items] == [
        "0001-26-000010",
        "0001-26-000009",
    ]
    assert any("cursor was not present" in warning for warning in batch.warnings)


def test_pipeline_restores_native_id_column_into_collector_cursor():
    row = SimpleNamespace(
        cursor={"page": 2},
        etag='"one"',
        last_modified=None,
        last_seen_native_id="0001-26-000008",
    )

    assert _cursor_payload(row) == {
        "page": 2,
        "etag": '"one"',
        "last_seen_native_id": "0001-26-000008",
    }
