"""Near-duplicate boundaries and revision classification."""

from __future__ import annotations

import math
from enum import StrEnum

from .contracts import EventStatus


class ClusterDecision(StrEnum):
    MERGE = "merge"
    LLM_REVIEW = "llm_review"
    KEEP_SEPARATE = "keep_separate"


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def cluster_decision(similarity: float, *, both_arxiv: bool = False, same_arxiv_id: bool = False) -> ClusterDecision:
    if both_arxiv and not same_arxiv_id:
        return ClusterDecision.KEEP_SEPARATE
    if similarity >= 0.92:
        return ClusterDecision.MERGE
    if similarity >= 0.84:
        return ClusterDecision.LLM_REVIEW
    return ClusterDecision.KEEP_SEPARATE


def change_summary(status: EventStatus) -> str:
    return {
        EventStatus.NEW_ENTITY: "首次收录",
        EventStatus.MATERIAL_UPDATE: "标题、核心摘要、版本或关键事实发生实质变化",
        EventStatus.MINOR_UPDATE: "页面或非关键元数据变化，仅更新归档",
        EventStatus.DISCOVERED_LATE: "来源时间早于本次采集窗口，属于迟发现事件",
    }[status]
