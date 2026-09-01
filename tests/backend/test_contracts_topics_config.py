from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_research_radar.cli import _runtime
from ai_research_radar.config import load_issuers, load_sources
from ai_research_radar.contracts import Topic
from ai_research_radar.db import IssuerMasterModel, sync_issuers
from ai_research_radar.pipeline import verification_for
from ai_research_radar.topics import RuleTopicClassifier, infer_event_type


ROOT = Path(__file__).parents[2]


def test_yaml_anchors_produce_independent_github_sources():
    sources = load_sources(ROOT / "configs")
    by_id = {source.id: source for source in sources}
    assert by_id["mcp-releases"].kind == "github_releases"
    assert by_id["a2a-releases"].fetch_strategy == "github_releases"
    assert by_id["a2a-releases"].url != by_id["mcp-releases"].url
    assert by_id["a2a-releases"].group == "standards"


def test_live_tech_fallback_sources_and_rss_caps_are_configured():
    by_id = {
        source.id: source for source in load_sources(ROOT / "configs")
    }
    for source_id in ("openai-news", "huggingface-blog"):
        assert by_id[source_id].kind == "rss"
        assert by_id[source_id].max_items == 100

    assert by_id["openai-news"].url == "https://openai.com/news/rss.xml"
    assert by_id["xai-hf"].enabled is True
    assert by_id["xai-hf"].entity_id == "xai"
    assert "author=xai-org" in by_id["xai-hf"].url
    assert by_id["cognition-blog"].enabled is True
    assert by_id["cursor-blog"].enabled is True
    assert by_id["perplexity-blog"].enabled is True
    assert by_id["physical-intelligence-blog"].enabled is True


def test_touch_high_seed_hits_all_five_technical_topics_and_company_claim():
    seed = json.loads((ROOT / "tests/fixtures/touch_high_seed.json").read_text())
    match = RuleTopicClassifier.from_config(ROOT / "configs").classify(
        seed["title"], seed["content"]
    )
    assert {topic.value for topic in match.topics} >= set(seed["expected_topics"])
    assert verification_for("official_company").value == seed["expected_verification_status"]


def test_weak_long_context_and_synthetic_data_do_not_match_alone():
    classifier = RuleTopicClassifier.from_config(ROOT / "configs")
    match = classifier.classify("A larger context window", "Long context and synthetic data")
    assert Topic.LONG_HORIZON not in match.topics
    assert Topic.SELF_EVOLVING not in match.topics


def test_issuer_master_syncs_versioned_identifiers(session):
    issuers = load_issuers(ROOT / "configs")
    assert sync_issuers(session, issuers) == len(issuers)
    coreweave = session.get(IssuerMasterModel, "coreweave")
    assert coreweave is not None
    assert coreweave.markets[0] == {"market": "NASDAQ", "ticker": "CRWV"}
    assert "CoreWeave" in coreweave.aliases
    openai = session.get(IssuerMasterModel, "openai")
    assert openai is not None
    assert "OpenAI" in openai.aliases


def test_production_runtime_refuses_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'unsafe.db'}")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "configs"))
    with pytest.raises(RuntimeError, match="requires a PostgreSQL"):
        _runtime()


def test_exchange_placing_title_maps_to_raise():
    assert (
        infer_event_type(
            "PLACING OF NEW H SHARES UNDER GENERAL MANDATE",
            "",
            kind="html",
        )
        == "RAISE"
    )
    assert infer_event_type("完成配售新H股", "", kind="html") == "RAISE"
