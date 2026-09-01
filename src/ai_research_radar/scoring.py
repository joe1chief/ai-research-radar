"""Transparent 100-point event scoring tuned for AI-native breakthroughs, research, and high-signal founder podcasts."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import EventStatus, VerificationStatus


EVIDENCE_SCORES = {
    "paper": 24,
    "regulatory_filing": 25,
    "exchange_filing": 25,
    "official_standard": 23,
    "official_repo": 24,
    "official_company": 22,
    "podcast_interview": 22,
    "reputable_media": 15,
    "media": 10,
}


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    topic_fit: int
    evidence: int
    novelty: int
    impact: int
    actionability: int
    penalties: int = 0

    @property
    def total(self) -> int:
        return max(
            0,
            min(
                100,
                self.topic_fit
                + self.evidence
                + self.novelty
                + self.impact
                + self.actionability
                - self.penalties,
            ),
        )


def score_event(
    *,
    topic_strength: int,
    evidence_type: str,
    status: EventStatus,
    verification: VerificationStatus,
    event_type: str,
    has_primary_url: bool = True,
    is_duplicate: bool = False,
    is_ordinary_commit: bool = False,
    has_artifact_link: bool = True,
) -> ScoreBreakdown:
    topic_fit = min(30, max(0, topic_strength))
    evidence = EVIDENCE_SCORES.get(evidence_type, 12)
    novelty = {
        EventStatus.NEW_ENTITY: 18,
        EventStatus.MATERIAL_UPDATE: 20,
        EventStatus.DISCOVERED_LATE: 10,
        EventStatus.MINOR_UPDATE: 3,
    }[status]
    high_impact = {
        "MODEL_RELEASE",
        "PODCAST_INTERVIEW",
        "FOUNDER_INTERVIEW",
        "REASONING_BREAKTHROUGH",
        "RLVR_BENCHMARK",
        "PROTOCOL_RELEASE",
        "IPO_FILING",
        "RAISE",
        "M_AND_A",
        "CAPEX_COMPUTE",
        "REGULATORY_EXPORT",
    }
    impact = 18 if event_type in high_impact else (14 if event_type in {"PAPER", "RELEASE", "RESEARCH_REPORT"} else 8)
    actionability = 10 if (evidence >= 22 and has_artifact_link) else 6
    penalties = 0
    if is_duplicate:
        penalties += 40
    if verification == VerificationStatus.REPORTED_UNCONFIRMED:
        penalties += 25
    if not has_primary_url:
        penalties += 20
    if is_ordinary_commit:
        penalties += 20
    return ScoreBreakdown(topic_fit, evidence, novelty, impact, actionability, penalties)


def alert_eligible(
    score: int,
    verification: VerificationStatus,
    event_type: str,
    *,
    paper_has_release_evidence: bool = False,
) -> bool:
    if score < 80 or verification == VerificationStatus.REPORTED_UNCONFIRMED:
        return False
    if event_type == "PAPER" and not paper_has_release_evidence:
        return False
    capital_types = {
        "IPO_FILING",
        "RAISE",
        "M_AND_A",
        "CAPEX_COMPUTE",
        "MATERIAL_CONTRACT",
        "EARNINGS_GUIDANCE",
        "OWNERSHIP",
        "REGULATORY_EXPORT",
    }
    if event_type in capital_types:
        return verification in {
            VerificationStatus.VERIFIED_PRIMARY,
            VerificationStatus.CORROBORATED,
        }
    if verification == VerificationStatus.COMPANY_CLAIM:
        # A first-party claim may alert only when it describes an observable
        # shipped artifact or major model release.
        return event_type in {"MODEL_RELEASE", "RELEASE", "PROTOCOL_RELEASE"}
    return verification in {
        VerificationStatus.VERIFIED_PRIMARY,
        VerificationStatus.CORROBORATED,
    }
