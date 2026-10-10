"""Bounded fixtures extracted from official listings; no live HTTP in tests."""

import json
from html import escape
from pathlib import Path

import httpx
import pytest

from ai_research_radar.config import load_sources
from ai_research_radar.collectors.html import HtmlListingCollector
from ai_research_radar.collectors.base import CollectorHTTPError

ROOT = Path(__file__).parents[2]
FIXTURES = json.loads((ROOT / "tests/fixtures/migrated_source_listings.json").read_text())
SPECS = {s.id: s for s in load_sources(ROOT / "configs")}


@pytest.mark.parametrize("source_id", list(FIXTURES))
def test_migrated_entries_restrict_article_paths_and_parse_details(source_id):
    fixture, spec = FIXTURES[source_id], SPECS[source_id]
    assert spec.url == fixture["entry_url"] and spec.enabled and spec.kind == "html"
    articles = fixture["articles"]
    listing = "".join(f'<a href="{r["url"]}">{escape(r["title"])}</a>' for r in articles)
    listing += "".join(
        f'<a href="{url}">Navigation item</a>'
        for url in [
            spec.url,
            spec.url + "?page=2",
            spec.url + "/category",
            spec.url + "/page/2",
            "https://unrelated.invalid/blog/article",
            "https://" + httpx.URL(spec.url).host + "/privacy",
        ]
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == spec.url:
            return httpx.Response(200, text=listing, headers={"content-type": "text/html"})
        assert str(request.url) in {r["url"] for r in articles}
        return httpx.Response(
            200,
            text='<meta property="og:title" content="Verified article"><article><h1>Verified article</h1><p>Research details with enough content to verify article extraction.</p></article>',
            headers={"content-type": "text/html"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = HtmlListingCollector(spec, client=client, user_agent="offline").collect({})
    assert {i.canonical_url for i in batch.items} == {r["url"] for r in articles}
    assert not batch.warnings
    assert len(calls) == 1 + len(articles)
    assert all("Research details" in i.content for i in batch.items)


def test_migration_does_not_relax_cross_site_redirect_guard():
    spec = SPECS["runway-blog"]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "https://unrelated.invalid/article"})
        )
    ) as client:
        with pytest.raises(CollectorHTTPError, match="cross-site"):
            HtmlListingCollector(spec, client=client, user_agent="offline").collect({})


def test_unresolved_and_working_rss_sources_remain_enabled_and_unchanged():
    expected = {
        "bair-blog": "https://bair.berkeley.edu/blog/feed.xml",
        "allen-ai-news": "https://allenai.org/feed.xml",
        "dwarkesh-podcast": "https://www.dwarkeshpatel.com/feed",
        "langchain-blog": "https://blog.langchain.dev/rss/",
    }
    for sid, url in expected.items():
        assert SPECS[sid].url == url and SPECS[sid].enabled and SPECS[sid].kind == "rss"
