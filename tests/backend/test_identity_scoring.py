from ai_research_radar.contracts import EventStatus, VerificationStatus
from ai_research_radar.dedupe import ClusterDecision, cluster_decision
from ai_research_radar.identity import (
    canonicalize_url,
    content_hash,
    normalize_content,
    parse_arxiv_identity,
    stable_id,
)
from ai_research_radar.scoring import alert_eligible, score_event


def test_url_and_content_identities_are_stable():
    assert canonicalize_url("HTTPS://Example.COM/a/?utm_source=x&b=2&a=1#frag") == (
        "https://example.com/a?a=1&b=2"
    )
    assert content_hash("Ａ  B\n") == content_hash("A B")
    assert stable_id("Source", "ABC") == stable_id("source", "abc")
    assert parse_arxiv_identity("https://arxiv.org/abs/2501.12345v3") == ("2501.12345", 3)


def test_content_normalization_removes_nul_without_joining_words():
    assert normalize_content("DeepSeek\x00release") == "DeepSeek release"
    assert "\x00" not in normalize_content("before&#0;after\x00done")


def test_cluster_thresholds_and_arxiv_boundary():
    assert cluster_decision(0.92) == ClusterDecision.MERGE
    assert cluster_decision(0.84) == ClusterDecision.LLM_REVIEW
    assert cluster_decision(0.839) == ClusterDecision.KEEP_SEPARATE
    assert cluster_decision(0.99, both_arxiv=True, same_arxiv_id=False) == (
        ClusterDecision.KEEP_SEPARATE
    )


def test_capital_company_claim_cannot_alert_but_release_can():
    assert not alert_eligible(95, VerificationStatus.COMPANY_CLAIM, "RAISE")
    assert alert_eligible(95, VerificationStatus.COMPANY_CLAIM, "MODEL_RELEASE")
    assert not alert_eligible(95, VerificationStatus.COMPANY_CLAIM, "PUBLICATION")
    assert not alert_eligible(95, VerificationStatus.VERIFIED_PRIMARY, "PAPER")
    assert alert_eligible(
        95,
        VerificationStatus.VERIFIED_PRIMARY,
        "PAPER",
        paper_has_release_evidence=True,
    )


def test_scoring_penalties_are_explicit():
    score = score_event(
        topic_strength=30,
        evidence_type="official_company",
        status=EventStatus.NEW_ENTITY,
        verification=VerificationStatus.REPORTED_UNCONFIRMED,
        event_type="PUBLICATION",
        is_duplicate=True,
    )
    assert score.penalties == 65
    assert score.total < 45
