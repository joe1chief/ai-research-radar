from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from datetime import date

import httpx

from ai_research_radar.raw_storage import RawSnapshotStore


def test_supabase_raw_store_compresses_uploads_and_deletes_by_private_path():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = RawSnapshotStore(
        supabase_url="https://project.supabase.co",
        secret_key="secret-test-key",
        client=client,
    )
    path = store.put(
        source_id="lab/blog",
        item_id="item:1",
        content_hash="a" * 64,
        payload=b"<html>private source response</html>",
        fetched_at=datetime(2026, 7, 12, 1, 2, tzinfo=UTC),
    )
    assert path is not None
    assert path.startswith("2026/07/12/lab-blog/item-1/")
    assert gzip.decompress(requests[0].content) == b"<html>private source response</html>"
    assert requests[0].headers["authorization"] == "Bearer secret-test-key"
    assert requests[0].headers["x-upsert"] == "false"

    store.delete([path])
    assert requests[1].method == "DELETE"
    assert json.loads(requests[1].content) == {"prefixes": [path]}


def test_raw_store_finds_expired_orphans_from_date_prefixes():
    tree = {
        "": [{"name": "2026", "id": None, "metadata": None}],
        "2026": [
            {"name": "06", "id": None, "metadata": None},
            {"name": "07", "id": None, "metadata": None},
        ],
        "2026/06": [{"name": "20", "id": None, "metadata": None}],
        "2026/07": [{"name": "10", "id": None, "metadata": None}],
        "2026/06/20": [{"name": "source", "id": None, "metadata": None}],
        "2026/06/20/source": [{"name": "item", "id": None, "metadata": None}],
        "2026/06/20/source/item": [
            {"name": "snapshot.html.gz", "id": "object-1", "metadata": {"size": 10}}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json=tree.get(payload["prefix"], []), request=request)

    store = RawSnapshotStore(
        supabase_url="https://project.supabase.co",
        secret_key="secret-test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert store.list_older_than(date(2026, 7, 1)) == [
        "2026/06/20/source/item/snapshot.html.gz"
    ]
