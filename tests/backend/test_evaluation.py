import json

import pytest

from ai_research_radar.contracts import Topic
from ai_research_radar.evaluation import evaluate_topic_labels, load_topic_labels
from ai_research_radar.llm import QwenResult
from ai_research_radar.topics import RuleTopicClassifier


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), "utf-8")


def test_topic_acceptance_is_fail_closed_when_review_set_is_too_small(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(
        path,
        [
            {
                "id": "one",
                "title": "Long Horizon Task for autonomous agents",
                "expected_top1": "long_horizon",
                "reviewed_by": "reviewer-a",
            }
        ],
    )
    result = evaluate_topic_labels(
        load_topic_labels(path),
        RuleTopicClassifier.from_config("configs"),
        minimum_samples=100,
    )
    assert result["top1_precision"] == 1.0
    assert result["enough_samples"] is False
    assert result["passed"] is False


def test_topic_acceptance_passes_only_at_requested_precision_and_size(tmp_path):
    path = tmp_path / "labels.jsonl"
    rows = [
        {
            "id": "long",
            "title": "Long Horizon Task for an agent",
            "expected_top1": "long_horizon",
            "reviewed_by": "reviewer-a",
        },
        {
            "id": "self",
            "title": "Self-play and RLVR for self-improving models",
            "expected_top1": "self_evolving",
            "reviewed_by": "reviewer-b",
        },
    ]
    _write(path, rows)
    result = evaluate_topic_labels(
        load_topic_labels(path),
        RuleTopicClassifier.from_config("configs"),
        minimum_samples=2,
        threshold=1.0,
    )
    assert result["passed"] is True
    assert result["mode"] == "rules_only"
    assert result["errors"] == []


def test_topic_acceptance_can_exercise_qwen_flash_selection(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(
        path,
        [
            {
                "id": "multi",
                "title": "Long Horizon Task for autonomous multi-agent systems",
                "expected_top1": "autonomous_agent",
                "reviewed_by": "reviewer-a",
            }
        ],
    )

    class FakeQwen:
        enabled = True

        def enhance(self, *_args, **_kwargs):
            return QwenResult(topics=[Topic.AUTONOMOUS_AGENT])

    result = evaluate_topic_labels(
        load_topic_labels(path),
        RuleTopicClassifier.from_config("configs"),
        minimum_samples=1,
        threshold=1.0,
        qwen=FakeQwen(),
    )
    assert result["passed"] is True
    assert result["mode"] == "qwen_flash"
    assert result["production_model_evaluated"] is True


def test_topic_labels_reject_duplicates_and_missing_human_review(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(
        path,
        [
            {
                "id": "duplicate",
                "title": "Agent memory",
                "expected_top1": "autonomous_agent",
                "reviewed_by": "reviewer-a",
            },
            {
                "id": "duplicate",
                "title": "Agent runtime",
                "expected_top1": "autonomous_agent",
                "reviewed_by": "reviewer-b",
            },
        ],
    )
    with pytest.raises(ValueError, match="duplicate label id"):
        load_topic_labels(path)

    _write(
        path,
        [
            {
                "id": "pending",
                "title": "Agent memory",
                "expected_top1": "autonomous_agent",
                "reviewed_by": "",
            }
        ],
    )
    with pytest.raises(ValueError, match="invalid label"):
        load_topic_labels(path)
