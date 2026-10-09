"""Regression coverage for CPU stalls while normalizing public source text."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_research_radar.identity import normalize_content


def _normalize_bounded(value: str) -> str:
    # A thread timeout cannot interrupt Python's regex engine. Run potentially
    # pathological inputs in a process so a regression fails instead of hanging CI.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from ai_research_radar.identity import normalize_content; "
            "print(normalize_content(sys.stdin.read()), end='')",
        ],
        input=value,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    return result.stdout


@pytest.mark.parametrize(
    "metric", ["best@16", "avg@16", "pass@k", "user@example", "user@media", "@media-example"]
)
def test_research_metrics_and_nested_code_are_not_css(metric):
    # Reduced from cognition.com/blog/kevin-32b: an @ metric precedes CUDA
    # blocks. The old CSS regex treated everything after @ as an at-rule and
    # exponentially backtracked when it reached nested braces.
    value = (
        f"{metric} reports correctness. kernel {{ "
        + "x" * 100
        + " { if (ready) { compute(); } } } End."
    )
    assert _normalize_bounded(value) == value


@pytest.mark.parametrize(
    ("css", "expected"),
    [
        ("@media screen { .card { color: red; } }", "Before After"),
        (
            "@media screen { @supports (display: grid) { .card { color: red; } } }",
            "Before After",
        ),
        ('@supports (display: grid) { .card::after { content: "}\\\"{"; } }', "Before After"),
        ("@font-face { font-family: Test; src: url(test.woff2); }", "Before After"),
        ("@keyframes spin { from { opacity: 0; } to { opacity: 1; } }", "Before After"),
        ("@layer base { .card { color: red; } } @page { margin: 0; }", "Before After"),
        ("@media screen { " + "x" * 100, "Before @media screen { " + "x" * 100 + " After"),
    ],
)
def test_css_at_rules_are_balanced_without_backtracking(css, expected):
    assert _normalize_bounded(f"Before {css} After") == expected


def test_markup_styles_and_plain_text_identity_behavior_remain_stable():
    value = """<p>Ａgent&nbsp;research</p><script>ignore()</script>
    <style>.card { color: red; }</style><noscript>hidden</noscript>
    <svg><text>icon</text></svg>/* comment */.card { color: red; }
    #title:hover { color: blue; }<p>before\x00after</p>"""
    assert normalize_content(value) == "Agent research before after"


def test_deep_css_nesting_uses_no_python_recursion():
    value = "Before @media screen {" + "{" * 2000 + "color: red;" + "}" * 2001 + " After"
    assert _normalize_bounded(value) == "Before After"


def test_html_detail_collection_preserves_research_metrics_and_reaches_next_page():
    script = """
import json
import httpx
from ai_research_radar.collectors.html import HtmlListingCollector
from ai_research_radar.contracts import SourceSpec

spec = SourceSpec(id='research', entity_id='lab', group='tech', kind='html',
    url='https://example.com/blog', fetch_strategy='html_listing',
    evidence_type='official_company', parser='html_links', detail_fetch_limit=2)
calls = []
body = 'best@16 reports correctness. kernel { ' + 'x' * 100 + ' { if (ready) { compute(); } } } End.'
def reply(request):
    calls.append(request.url.path)
    page = ('<a href="/blog/one">First research</a><a href="/blog/two">Second research</a>'
        if request.url.path == '/blog' else '<article><p>' + body + '</p></article>')
    return httpx.Response(200, text=page, headers={'content-type': 'text/html'}, request=request)
with httpx.Client(transport=httpx.MockTransport(reply)) as client:
    batch = HtmlListingCollector(spec, client=client).collect()
print(json.dumps({'calls': calls, 'content': [item.content for item in batch.items], 'warnings': batch.warnings}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    payload = json.loads(result.stdout)
    assert payload["calls"] == ["/blog", "/blog/one", "/blog/two"]
    assert len(payload["content"]) == 2
    assert all(
        "best@16" in text and "compute();" in text and text.endswith("End.")
        for text in payload["content"]
    )
    assert payload["warnings"] == []
