from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from ai_research_radar.collectors.arxiv import ArxivCollector
from ai_research_radar.collectors.arxiv_oai import ArxivOAICollector
from ai_research_radar.collectors.base import CollectorHTTPError, DomainRequestThrottle
from ai_research_radar.collectors.github import GitHubReleaseCollector
from ai_research_radar.collectors.html import HtmlListingCollector
from ai_research_radar.collectors.huggingface import HuggingFaceModelsCollector
from ai_research_radar.collectors.rss import RSSCollector
from ai_research_radar.collectors.sec import SECSubmissionsCollector
from ai_research_radar.collectors.sitemap import SitemapCollector
from ai_research_radar.collectors.sse import SSEAnnouncementsCollector
from ai_research_radar.contracts import SourceSpec
from ai_research_radar.identity import stable_id
from ai_research_radar.pipeline import _source_authorization


def spec(kind: str, *, url: str = "https://example.com/feed") -> SourceSpec:
    return SourceSpec(
        id=f"test-{kind}",
        entity_id="example",
        group="tech",
        kind=kind,
        url=url,
        fetch_strategy=kind,
        cadence="daily",
        evidence_type="official_company",
        parser="json",
    )


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rss_etag_and_304():
    seen_headers = []

    def handler(request):
        seen_headers.append(request.headers)
        if request.headers.get("If-None-Match"):
            return httpx.Response(304, request=request)
        xml = """<rss><channel><title>Lab</title><item><guid>x1</guid><title>Agent release</title>
        <link>https://example.com/post?utm_source=rss</link><description>multi-agent runtime</description>
        <pubDate>Sat, 12 Jul 2026 01:00:00 GMT</pubDate></item></channel></rss>"""
        return httpx.Response(200, text=xml, headers={"ETag": '"v1"'}, request=request)

    collector = RSSCollector(spec("rss"), client=client(handler))
    first = collector.collect()
    assert first.items[0].external_id == "x1"
    assert first.items[0].canonical_url == "https://example.com/post"
    second = collector.collect(first.cursor)
    assert second.not_modified
    assert seen_headers[1]["If-None-Match"] == '"v1"'


def test_collector_identity_overrides_shared_client_default():
    seen_user_agents: list[str] = []

    def handler(request):
        seen_user_agents.append(request.headers["User-Agent"])
        return httpx.Response(200, text="<rss><channel/></rss>", request=request)

    shared = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "shared-client-default"},
    )
    RSSCollector(
        spec("rss"),
        client=shared,
        user_agent="AIResearchRadar/scoped",
    ).collect()

    assert seen_user_agents == ["AIResearchRadar/scoped"]


def test_rss_max_items_caps_feed_in_source_order():
    xml = """<rss><channel><title>Lab</title>
    <item><guid>x1</guid><title>Newest</title><link>https://example.com/1</link></item>
    <item><guid>x2</guid><title>Middle</title><link>https://example.com/2</link></item>
    <item><guid>x3</guid><title>Oldest</title><link>https://example.com/3</link></item>
    </channel></rss>"""
    capped_spec = SourceSpec(
        **{**spec("rss").model_dump(), "max_items": 2}
    )
    batch = RSSCollector(
        capped_spec,
        client=client(lambda request: httpx.Response(200, text=xml, request=request)),
    ).collect()
    assert [item.external_id for item in batch.items] == ["x1", "x2"]


def test_arxiv_version_and_links():
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/abs/2501.12345v2</id><updated>2026-07-12T01:00:00Z</updated>
      <published>2026-07-11T01:00:00Z</published><title>Long-Horizon Agent</title>
      <summary>long-term planning for an agent</summary><author><name>A. Author</name></author>
      <link href="https://arxiv.org/abs/2501.12345v2" rel="alternate"/>
      <category term="cs.AI"/></entry></feed>"""
    c = client(lambda request: httpx.Response(200, text=atom, request=request))
    item = ArxivCollector(spec("arxiv_api", url="https://export.arxiv.org/api/query"), client=c).collect().items[0]
    assert item.external_id == "2501.12345"
    assert item.metadata["version"] == 2
    assert item.metadata["authors"] == ["A. Author"]
    assert item.metadata["alphaxiv_url"].endswith("2501.12345")


def test_arxiv_api_paginates_and_oai_exposes_version_history():
    def atom(arxiv_id, version):
        return f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>http://arxiv.org/abs/{arxiv_id}v{version}</id>
        <updated>2026-07-12T0{version}:00:00Z</updated><published>2026-07-12T00:00:00Z</published>
        <title>Agent memory {version}</title><summary>autonomous agent memory</summary>
        <link href="https://arxiv.org/abs/{arxiv_id}v{version}" rel="alternate"/>
        </entry></feed>"""

    def paged(request):
        start = int(request.url.params.get("start", "0"))
        return httpx.Response(200, text=atom(f"2607.0000{start + 1}", 1), request=request)

    paged_spec = SourceSpec(
        **{
            **spec("arxiv_api", url="https://export.arxiv.org/api/query").model_dump(),
            "page_size": 1,
            "max_pages": 2,
            "request_interval_seconds": 0,
        }
    )
    paged_batch = ArxivCollector(paged_spec, client=client(paged)).collect()
    paged_items = paged_batch.items
    assert {item.external_id for item in paged_items} == {"2607.00001", "2607.00002"}
    assert "arXiv page budget reached" in paged_batch.warnings[0]

    oai = """<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <responseDate>2026-07-12T02:00:00Z</responseDate><ListRecords><record><header>
      <identifier>oai:arXiv.org:2607.00001</identifier><datestamp>2026-07-12</datestamp></header>
      <metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>2607.00001</id>
      <version version="v1"><date>Fri, 10 Jul 2026 00:00:00 GMT</date><size>1kb</size></version>
      <version version="v2"><date>Sun, 12 Jul 2026 00:00:00 GMT</date><size>2kb</size></version>
      <title>Agent Memory</title><authors>A. One and B. Two</authors><categories>cs.AI</categories>
      <abstract>long-horizon autonomous agent memory</abstract></arXivRaw></metadata></record>
      <resumptionToken></resumptionToken></ListRecords></OAI-PMH>"""
    oai_spec = SourceSpec(
        **{
            **spec("arxiv_oai", url="https://oaipmh.arxiv.org/oai").model_dump(),
            "sets": ["cs:cs:AI"],
            "max_pages": 1,
        }
    )
    oai_item = ArxivOAICollector(
        oai_spec, client=client(lambda request: httpx.Response(200, text=oai, request=request))
    ).collect().items[0]
    assert oai_item.external_id == "2607.00001"
    assert oai_item.metadata["version"] == 2
    assert len(oai_item.metadata["version_history"]) == 2


def test_github_html_huggingface_and_sec_parsers():
    release = [{"id": 42, "tag_name": "v1.0", "html_url": "https://github.com/a/b/releases/tag/v1", "body": "agent runtime", "published_at": "2026-07-12T00:00:00Z"}]
    github = GitHubReleaseCollector(
        spec("github_releases", url="https://api.github.com/repos/a/b/releases"),
        client=client(lambda request: httpx.Response(200, json=release, request=request)),
    ).collect()
    assert github.items[0].metadata["tag_name"] == "v1.0"

    html = '<a href="/research/agent-memory">Agent memory architecture</a><a href="https://evil.test/x">Ignore me</a>'
    listing = HtmlListingCollector(
        spec("html", url="https://example.com/research"),
        client=client(lambda request: httpx.Response(200, text=html, request=request)),
    ).collect()
    assert [item.canonical_url for item in listing.items] == ["https://example.com/research/agent-memory"]

    detail_spec = SourceSpec(
        **{
            **spec("html", url="https://example.com/research").model_dump(),
            "detail_fetch_limit": 1,
        }
    )

    def html_detail(request):
        if request.url.path == "/research":
            return httpx.Response(200, text=html, request=request)
        detail = """<html><head><script type="application/ld+json">{
        "@type":"BlogPosting","@id":"post-1","url":"https://example.com/research/agent-memory",
        "headline":"Persistent Agent Memory","description":"long-horizon autonomous agent",
        "datePublished":"2026-07-12T01:00:00Z","dateModified":"2026-07-12T02:00:00Z"
        }</script></head><body><article><p>New memory architecture and experiments.</p>
        <a href="https://github.com/example/memory">Code</a></article></body></html>"""
        return httpx.Response(200, text=detail, headers={"content-type": "text/html"}, request=request)

    detailed = HtmlListingCollector(detail_spec, client=client(html_detail)).collect().items[0]
    assert detailed.title == "Persistent Agent Memory"
    assert "New memory architecture" in detailed.content
    assert detailed.metadata["source_native_id"] == "post-1"
    assert detailed.metadata["code_url"] == "https://github.com/example/memory"
    assert detailed.raw_snapshot is not None
    assert b"application/ld+json" in detailed.raw_snapshot
    assert "raw_snapshot" not in detailed.model_dump()

    models = [{"modelId": "org/model", "lastModified": "2026-07-12T00:00:00Z", "tags": ["agents"]}]
    hf = HuggingFaceModelsCollector(
        spec("huggingface_models", url="https://huggingface.co/api/models"),
        client=client(lambda request: httpx.Response(200, json=models, request=request)),
    ).collect()
    assert hf.items[0].external_id == "org/model"

    sec_payload = {
        "cik": "1652044",
        "name": "Alphabet Inc.",
        "filings": {"recent": {
            "accessionNumber": ["0001-26-000001", "0001-26-000002"],
            "form": ["8-K", "DEF 14A"],
            "filingDate": ["2026-07-11", "2026-07-10"],
            "acceptanceDateTime": ["2026-07-11T12:00:00Z", "2026-07-10T12:00:00Z"],
            "primaryDocument": ["x.htm", "proxy.htm"],
            "primaryDocDescription": ["Current report", "Proxy"],
        }},
    }
    sec = SECSubmissionsCollector(
        spec("sec_submissions", url="https://data.sec.gov/submissions/CIK0001652044.json"),
        client=client(lambda request: httpx.Response(200, json=sec_payload, request=request)),
        now=lambda: datetime.fromisoformat("2026-07-12T00:00:00+00:00"),
    ).collect()
    assert len(sec.items) == 1
    assert sec.items[0].external_id == "0001-26-000001"
    assert "/Archives/edgar/data/1652044/000126000001/x.htm" in sec.items[0].canonical_url


def test_cross_site_redirect_is_rejected():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.test/feed"}, request=request)

    collector = RSSCollector(spec("rss"), client=client(handler), max_attempts=1)
    with pytest.raises(CollectorHTTPError, match="cross-site") as caught:
        collector.collect()
    assert caught.value.status_code == 302


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_non_rate_limit_client_errors_fail_without_retry_and_are_redacted(status_code):
    calls = 0
    sleeps: list[float] = []

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            text="provider body containing private-token",
            request=request,
        )

    collector = RSSCollector(
        spec("rss", url="https://example.com/private/feed?api_key=private-token"),
        client=client(handler),
        max_attempts=3,
        sleep=sleeps.append,
    )
    with pytest.raises(CollectorHTTPError) as caught:
        collector.collect()

    assert calls == 1
    assert sleeps == []
    assert caught.value.status_code == status_code
    assert caught.value.retryable is False
    assert caught.value.host == "example.com"
    assert "private-token" not in str(caught.value)
    assert "/private/feed" not in str(caught.value)


def test_rate_limit_and_server_errors_retry_before_success():
    statuses = iter([429, 503, 200])
    sleeps: list[float] = []

    def handler(request):
        return httpx.Response(next(statuses), text="<rss><channel/></rss>", request=request)

    collector = RSSCollector(
        spec("rss"),
        client=client(handler),
        max_attempts=3,
        sleep=sleeps.append,
    )

    batch = collector.collect()

    assert batch.items == []
    assert len(sleeps) == 2
    assert collector.last_http_status == 200


def test_server_error_exhaustion_keeps_structured_retryable_status():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="private provider response", request=request)

    collector = RSSCollector(
        spec("rss", url="https://feeds.example.com/rss"),
        client=client(handler),
        max_attempts=2,
        sleep=lambda _delay: None,
    )
    with pytest.raises(CollectorHTTPError) as caught:
        collector.collect()

    assert calls == 2
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert caught.value.host == "feeds.example.com"
    assert "private provider response" not in str(caught.value)


def test_timeout_retries_and_final_failure_exposes_only_safe_fields():
    calls = 0
    sleeps: list[float] = []

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private-token in transport message", request=request)

    collector = RSSCollector(
        spec("rss", url="https://feeds.example.com/rss?token=private-token"),
        client=client(handler),
        max_attempts=2,
        sleep=sleeps.append,
    )
    with pytest.raises(CollectorHTTPError) as caught:
        collector.collect()

    assert calls == 2
    assert len(sleeps) == 1
    assert caught.value.status_code is None
    assert caught.value.retryable is True
    assert caught.value.host == "feeds.example.com"
    assert "private-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_remote_protocol_error_retries_without_retaining_request_details():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.RemoteProtocolError(
            "private-token in remote protocol message",
            request=request,
        )

    collector = RSSCollector(
        spec("rss", url="https://feeds.example.com/rss?token=private-token"),
        client=client(handler),
        max_attempts=2,
        sleep=lambda _delay: None,
    )
    with pytest.raises(CollectorHTTPError) as caught:
        collector.collect()

    assert calls == 2
    assert caught.value.retryable is True
    assert "private-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_domain_throttle_shares_slots_across_sec_subdomains():
    class FakeTime:
        now = 100.0

        def __init__(self):
            self.sleeps: list[float] = []

        def monotonic(self):
            return self.now

        def sleep(self, delay):
            self.sleeps.append(delay)
            self.now += delay

    fake = FakeTime()
    throttle = DomainRequestThrottle(
        0.25,
        clock=fake.monotonic,
        sleep=fake.sleep,
    )

    throttle.wait("https://data.sec.gov/submissions/one.json")
    throttle.wait("https://www.sec.gov/Archives/two.htm")
    throttle.wait("https://example.com/unrelated")

    assert fake.sleeps == [pytest.approx(0.25)]


def test_html_source_patterns_exclude_navigation_and_mark_routine_filings():
    filtered = SourceSpec(
        **{
            **spec("html", url="https://issuer.example/investors").model_dump(),
            "include_url_patterns": [r"\.pdf(?:$|\?)"],
            "routine_title_patterns": [r"monthly return"],
            "max_items": 1,
            "detail_fetch_limit": 0,
        }
    )
    page = """
    <a href="/products/model">Model announcement</a>
    <a href="/filings/monthly.pdf">Monthly return PDF</a>
    <a href="/filings/material.pdf">Material contract PDF</a>
    """
    items = HtmlListingCollector(
        filtered,
        client=client(lambda request: httpx.Response(200, text=page, request=request)),
    ).collect().items
    assert len(items) == 1
    assert items[0].canonical_url.endswith("monthly.pdf")
    assert items[0].metadata["routine"] is True


def test_html_detail_rejects_javascript_and_cross_site_structured_urls():
    detail_spec = SourceSpec(
        **{
            **spec("html", url="https://example.com/research").model_dump(),
            "detail_fetch_limit": 1,
        }
    )
    listing = '<a href="/research/safe">Safe agent research</a>'

    def handler(request):
        if request.url.path == "/research":
            return httpx.Response(200, text=listing, request=request)
        page = """<html><head><meta property="og:url" content="javascript:alert(1)"></head>
        <body><article><p>autonomous agent safety</p>
        <a href="javascript:github.com/evil">fake code</a></article></body></html>"""
        return httpx.Response(
            200,
            text=page,
            headers={"content-type": "text/html"},
            request=request,
        )

    item = HtmlListingCollector(detail_spec, client=client(handler)).collect().items[0]
    assert item.canonical_url == "https://example.com/research/safe"
    assert item.metadata["code_url"] is None


def test_html_detail_failure_warning_does_not_persist_url_or_query():
    detail_spec = SourceSpec(
        **{
            **spec("html", url="https://example.com/news").model_dump(),
            "detail_fetch_limit": 1,
        }
    )
    listing = '<a href="/news/release?access_token=private-token">Release</a>'

    def handler(request):
        if request.url.path == "/news":
            return httpx.Response(200, text=listing, request=request)
        return httpx.Response(403, text="private-token", request=request)

    batch = HtmlListingCollector(
        detail_spec,
        client=client(handler),
        max_attempts=1,
    ).collect()

    assert batch.warnings == ["detail fetch failed: error_type=CollectorHTTPError"]
    assert "private-token" not in batch.warnings[0]
    assert "/news/release" not in batch.warnings[0]


def test_html_detail_deduplicates_redirected_canonical_collisions_in_listing_order():
    detail_spec = SourceSpec(
        **{
            **spec("html", url="https://example.com/news/").model_dump(),
            "detail_fetch_limit": 2,
        }
    )
    listing = """
    <a href="/news/alias-first">First release alias</a>
    <a href="/news/alias-second">Second release alias</a>
    """
    detail = """<html><body><article><p>Canonical release detail.</p></article></body></html>"""

    def handler(request):
        if request.url.path == "/news/":
            return httpx.Response(200, text=listing, request=request)
        if request.url.path in {"/news/alias-first", "/news/alias-second"}:
            return httpx.Response(
                302,
                headers={"Location": "/news/canonical-release"},
                request=request,
            )
        return httpx.Response(
            200,
            text=detail,
            headers={"content-type": "text/html"},
            request=request,
        )

    items = HtmlListingCollector(detail_spec, client=client(handler)).collect().items

    assert len(items) == 1
    assert items[0].canonical_url == "https://example.com/news/canonical-release"
    assert items[0].title == "First release alias"


def test_html_canonical_dedupe_preserves_first_identity_and_merges_detail_content():
    detail_spec = SourceSpec(
        **{
            **spec("html", url="https://example.com/news/").model_dump(),
            "detail_fetch_limit": 1,
        }
    )
    listing = """
    <a href="/news/canonical-release">Plain entry</a>
    <a href="/news/model-release-alias">Model release article</a>
    """
    detail = """<html><head><meta property="og:url"
      content="https://example.com/news/canonical-release"></head>
      <body><article><p>Canonical release detail.</p></article></body></html>"""

    def handler(request):
        if request.url.path == "/news/":
            return httpx.Response(200, text=listing, request=request)
        return httpx.Response(
            200,
            text=detail,
            headers={"content-type": "text/html"},
            request=request,
        )

    items = HtmlListingCollector(detail_spec, client=client(handler)).collect().items

    assert len(items) == 1
    assert items[0].title == "Plain entry"
    assert items[0].external_id == stable_id(
        "https://example.com/news/canonical-release"
    )
    assert items[0].content == "Canonical release detail."


def test_source_authorization_is_scoped_to_the_intended_api():
    assert _source_authorization(
        "openreview_api",
        github_token="github-secret",
        openreview_access_token="openreview-secret",
    ) == "Bearer openreview-secret"
    assert _source_authorization(
        "github_releases",
        github_token="github-secret",
        openreview_access_token="openreview-secret",
    ) == "Bearer github-secret"
    assert _source_authorization(
        "rss",
        github_token="github-secret",
        openreview_access_token="openreview-secret",
    ) is None


def test_sitemap_collector_keeps_only_configured_ai_discovery_urls():
    sitemap_spec = SourceSpec(
        **{
            **spec("sitemap", url="https://media.example/sitemap.xml").model_dump(),
            "include_url_patterns": [r"openai|artificial-intelligence"],
            "max_items": 10,
        }
    )
    xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://media.example/business/openai-funding-2026-07-12/</loc>
      <lastmod>2026-07-12T01:00:00Z</lastmod></url>
      <url><loc>https://media.example/sports/tennis-final-2026-07-12/</loc></url>
    </urlset>"""
    batch = SitemapCollector(
        sitemap_spec,
        client=client(lambda request: httpx.Response(200, text=xml, request=request)),
    ).collect()
    assert len(batch.items) == 1
    assert batch.items[0].title == "openai funding"
    assert batch.items[0].metadata["discovery_only"] is True


def test_sse_collector_paginates_date_window_and_emits_stable_official_documents():
    requests: list[httpx.Request] = []
    page_one = {
        "pageHelp": {"pageCount": 2, "pageNo": 1, "pageSize": 2, "total": 3},
        "result": [
            {
                "ADDDATE": "2026-07-11 19:30:00",
                "BULLETIN_HEADING": "临时公告",
                "BULLETIN_TYPE": "其它",
                "SECURITY_CODE": "688256",
                "SECURITY_NAME": "寒武纪",
                "SSEDATE": "2026-07-12",
                "TITLE": "关于签署重大算力合同的公告",
                "URL": (
                    "/disclosure/listedinfo/announcement/c/new/2026-07-12/"
                    "688256_20260712_ABCD.pdf"
                ),
            },
            {
                "SECURITY_CODE": "688256",
                "SECURITY_NAME": "寒武纪",
                "SSEDATE": "2026-07-12",
                "TITLE": "untrusted foreign document",
                "URL": "https://evil.example/filing.pdf",
            },
        ],
    }
    page_two = {
        "pageHelp": {"pageCount": "2", "pageNo": 2, "pageSize": 2, "total": "3"},
        "result": [
            {
                "ADDDATE": "2026-07-10 18:00:00",
                "BULLETIN_HEADING": "临时公告",
                "BULLETIN_TYPE": "公司治理",
                "SECURITY_CODE": "688256",
                "SECURITY_NAME": "寒武纪",
                "SSEDATE": "2026-07-11",
                "TITLE": "关于召开2026年第一次临时股东大会的公告",
                "URL": (
                    "/disclosure/listedinfo/announcement/c/new/2026-07-11/"
                    "688256_20260711_EFGH.pdf?cache=1"
                ),
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page_no = int(request.url.params["pageHelp.pageNo"])
        payload = page_one if page_no == 1 else page_two
        return httpx.Response(200, json=payload, request=request)

    sse_spec = SourceSpec(
        **{
            **spec(
                "sse_announcements",
                url="https://query.sse.com.cn/security/stock/queryCompanyBulletin.do",
            ).model_dump(),
            "id": "sse-cambricon",
            "entity_id": "cambricon",
            "group": "capital",
            "evidence_type": "exchange_filing",
            "product_id": "688256",
            "lookback_days": 14,
            "page_size": 2,
            "max_pages": 5,
            "routine_title_patterns": [r"关于召开.*股东大会"],
        }
    )
    collector = SSEAnnouncementsCollector(
        sse_spec,
        client=client(handler),
        now=lambda: datetime.fromisoformat("2026-07-12T13:00:00+08:00"),
    )
    batch = collector.collect()

    assert len(requests) == 2
    assert [request.url.params["pageHelp.beginPage"] for request in requests] == ["1", "2"]
    assert all(request.url.params["beginDate"] == "2026-06-29" for request in requests)
    assert all(request.url.params["endDate"] == "2026-07-12" for request in requests)
    assert all(
        request.headers["Referer"]
        == "https://www.sse.com.cn/assortment/stock/list/info/announcement/"
        for request in requests
    )

    assert [item.external_id for item in batch.items] == [
        "688256_20260712_ABCD",
        "688256_20260711_EFGH",
    ]
    first, second = batch.items
    assert first.canonical_url == (
        "https://www.sse.com.cn/disclosure/listedinfo/announcement/c/new/"
        "2026-07-12/688256_20260712_ABCD.pdf"
    )
    assert first.evidence_type == "exchange_filing"
    assert first.metadata["document_id"] == first.external_id
    assert first.metadata["exchange"] == "SSE"
    assert first.metadata["ticker"] == "688256"
    assert first.updated_at.isoformat() == "2026-07-11T11:30:00+00:00"
    assert second.canonical_url.endswith("688256_20260711_EFGH.pdf")
    assert second.metadata["routine"] is True
    assert batch.cursor["window_start"] == "2026-06-29"
    assert batch.cursor["window_end"] == "2026-07-12"
    assert batch.cursor["last_seen_native_id"] == first.external_id
    assert any("skipped 1 malformed rows" in warning for warning in batch.warnings)
