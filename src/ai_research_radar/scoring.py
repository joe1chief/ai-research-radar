"""Transparent 100-point event scoring."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import EventStatus, VerificationStatus


EVIDENCE_SCORES = {
    "paper": 22,
    "regulatory_filing": 25,
    "exchange_filing": 25,
    "official_standard": 23,
    "official_repo": 22,
    "official_company": 20,
    "reputable_media": 12,
    "media": 8,
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
) -> ScoreBreakdown:
    topic_fit = min(30, max(0, topic_strength))
    evidence = EVIDENCE_SCORES.get(evidence_type, 10)
    novelty = {
        EventStatus.NEW_ENTITY: 18,
        EventStatus.MATERIAL_UPDATE: 20,
        EventStatus.DISCOVERED_LATE: 10,
        EventStatus.MINOR_UPDATE: 3,
    }[status]
    high_impact = {
        "IPO_FILING",
        "RAISE",
        "M_AND_A",
        "CAPEX_COMPUTE",
        "REGULATORY_EXPORT",
        "MODEL_RELEASE",
    }
    impact = 15 if event_type in high_impact else (12 if event_type in {"PAPER", "RELEASE"} else 8)
    actionability = 10 if evidence >= 22 else 6
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
        # shipped artifact. Ordinary company posts remain digest-only even if
        # their topical score is high.
        return event_type in {"MODEL_RELEASE", "RELEASE"}
    return verification in {
        VerificationStatus.VERIFIED_PRIMARY,
        VerificationStatus.CORROBORATED,
    }
