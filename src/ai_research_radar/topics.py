"""Deterministic topic and cross-tag rules; the mandatory offline fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import load_topics
from .contracts import Topic
from .identity import normalize_content


CHINESE_ALIASES: dict[str, list[str]] = {
    Topic.LONG_HORIZON.value: [
        "长程任务",
        "长期规划",
        "持续执行",
        "分层任务拆解",
        "持久记忆",
        "新记忆架构",
    ],
    Topic.AUTONOMOUS_AGENT.value: [
        "自治智能体",
        "自主智能体",
        "多智能体",
        "数字员工",
        "智能体系统",
    ],
    Topic.SELF_EVOLVING.value: [
        "完全自我训练",
        "自我训练",
        "自我演化",
        "自我进化",
        "递归自我改进",
        "合成数据工厂",
        "ai 训练 ai",
    ],
    Topic.MECHANISTIC_INTERPRETABILITY.value: [
        "机械可解释性",
        "机制可解释性",
        "神经元逻辑",
        "因果追踪",
        "激活修补",
    ],
    Topic.SAFETY_GOVERNANCE.value: [
        "极致安全治理",
        "超级对齐",
        "价值对齐",
        "防滥用",
        "ai 治理",
        "安全治理",
    ],
    Topic.INDUSTRIAL_CAPITAL.value: [
        "融资",
        "上市申请",
        "招股书",
        "并购",
        "算力投资",
        "重大合同",
        "财报指引",
    ],
}

CROSS_TAG_TERMS: dict[str, list[str]] = {
    "agi_to_asi": ["agi to asi", "agi→asi", "agi 到 asi", "超级智能"],
    "scaling_agi": ["scaling agi", "扩展 agi"],
    "recursive_improvement": ["recursive improvement", "递归自我改进"],
    "collective_intelligence": ["collective intelligence", "多智能体社会", "集体智能"],
    "open_models": ["open model", "open-weight", "开放模型", "开源模型"],
    "long_context": ["long context", "long-context", "长上下文"],
    "llm_os": ["llm os", "模型操作系统"],
    "ai_native": ["ai-native", "ai native", "ai 原生"],
    "ai_for_science": ["ai for science", "药物设计", "科学智能"],
    "cyber_security": ["cyber", "vulnerability", "网络安全", "漏洞"],
    "compute_infrastructure": ["compute infrastructure", "gpu cluster", "算力基础设施", "智算"],
}


@dataclass(slots=True)
class TopicMatch:
    topics: list[Topic]
    strengths: dict[str, int] = field(default_factory=dict)
    matched_terms: dict[str, list[str]] = field(default_factory=dict)
    cross_tags: list[str] = field(default_factory=list)


class RuleTopicClassifier:
    def __init__(self, document: dict[str, Any]) -> None:
        self.rules = document.get("topics", [])

    @classmethod
    def from_config(cls, config_dir: Path | str = "configs") -> RuleTopicClassifier:
        return cls(load_topics(config_dir))

    def classify(self, title: str, text: str, *, event_type: str | None = None) -> TopicMatch:
        haystack = normalize_content(f"{title}\n{text}").casefold()
        topics: list[Topic] = []
        strengths: dict[str, int] = {}
        matched: dict[str, list[str]] = {}

        for raw_rule in self.rules:
            topic_id = raw_rule["id"]
            hard = [str(term).casefold() for term in raw_rule.get("hard_keywords", [])]
            hard += [term.casefold() for term in CHINESE_ALIASES.get(topic_id, [])]
            boosters = [str(term).casefold() for term in raw_rule.get("boosters", [])]
            weak_only = [str(term).casefold() for term in raw_rule.get("weak_only", [])]
            found_hard = [term for term in hard if _contains(haystack, term)]
            found_boosters = [term for term in boosters if _contains(haystack, term)]
            found_weak = [term for term in weak_only if _contains(haystack, term)]

            event_types = set(raw_rule.get("event_types", []))
            event_match = bool(event_type and event_type in event_types)
            qualifies = bool(found_hard or event_match)
            required = [str(term).casefold() for term in raw_rule.get("required_any", [])]
            # The seed itself explicitly names Long Horizon Task; otherwise enforce agent/model/robot.
            explicit_long_horizon = "long horizon task" in haystack or "长程任务" in haystack
            if required and qualifies and not explicit_long_horizon:
                qualifies = any(_contains(haystack, term) for term in required)
            if found_weak and not found_hard and not event_match:
                qualifies = False
            if not qualifies:
                continue
            try:
                topic = Topic(topic_id)
            except ValueError:
                continue
            topics.append(topic)
            strengths[topic_id] = min(30, 16 + len(found_hard) * 4 + len(found_boosters) * 2)
            matched[topic_id] = found_hard + found_boosters + ([event_type] if event_match else [])

        # Mechanistic interpretability is a child view, but both cards must be discoverable.
        if Topic.MECHANISTIC_INTERPRETABILITY in topics and Topic.SAFETY_GOVERNANCE not in topics:
            topics.append(Topic.SAFETY_GOVERNANCE)
            strengths[Topic.SAFETY_GOVERNANCE.value] = 14
            matched[Topic.SAFETY_GOVERNANCE.value] = ["parent:mechanistic_interpretability"]

        cross_tags = [
            tag
            for tag, terms in CROSS_TAG_TERMS.items()
            if any(_contains(haystack, term.casefold()) for term in terms)
        ]
        return TopicMatch(topics, strengths, matched, cross_tags)


def _contains(haystack: str, needle: str) -> bool:
    if re.fullmatch(r"[a-z0-9_-]+", needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


CAPITAL_EVENT_TERMS: dict[str, list[str]] = {
    "IPO_FILING": ["ipo", "listing application", "prospectus", "招股书", "上市申请"],
    "RAISE": [
        "funding round",
        "raised",
        "financing",
        "placing",
        "placement",
        "subscription of new shares",
        "allotment",
        "融资",
        "募资",
        "配售",
        "增发",
        "认购新股",
    ],
    "M_AND_A": ["acquisition", "merger", "takeover", "收购", "并购", "合并"],
    "CAPEX_COMPUTE": ["capital expenditure", "data center", "gpu cluster", "算力投资", "资本开支"],
    "MATERIAL_CONTRACT": ["material contract", "重大合同"],
    "EARNINGS_GUIDANCE": ["earnings guidance", "revenue guidance", "业绩指引", "盈利预告"],
    "OWNERSHIP": ["beneficial ownership", "shareholding", "持股", "权益变动"],
    "REGULATORY_EXPORT": ["export control", "regulatory action", "出口管制", "监管决定"],
}


def infer_event_type(title: str, text: str, *, kind: str = "") -> str:
    haystack = normalize_content(f"{title} {text}").casefold()
    for event_type, terms in CAPITAL_EVENT_TERMS.items():
        matched = any(term.casefold() in haystack for term in terms)
        if event_type == "CAPEX_COMPUTE":
            matched = matched and any(
                term in haystack
                for term in (
                    "gpu",
                    "accelerator",
                    "ai infrastructure",
                    "artificial intelligence infrastructure",
                    "data center",
                    "datacenter",
                    "算力",
                    "智算",
                )
            )
        if matched:
            return event_type
    if kind in {"arxiv_api", "arxiv_oai", "openreview_api", "acl_anthology", "pmlr"}:
        return "PAPER"
    if kind == "github_releases":
        return "RELEASE"
    if kind == "huggingface_models":
        return "MODEL_RELEASE"
    return "PUBLICATION"
