"""Fail-closed evaluation helpers for the human-labelled topic acceptance set."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .contracts import CollectedItem, Topic
from .llm import QwenClient
from .topics import RuleTopicClassifier


class TopicLabel(BaseModel):
    """One independently reviewed Top-1 topic judgement."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = ""
    event_type: str | None = None
    expected_top1: Topic | None
    reviewed_by: str = Field(
        min_length=1,
        description="Human reviewer handle; synthetic or pending rows must stay out of acceptance.",
    )


def load_topic_labels(path: Path) -> list[TopicLabel]:
    labels: list[TopicLabel] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            label = TopicLabel.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(f"invalid label at {path}:{line_number}: {exc}") from exc
        if label.id in seen:
            raise ValueError(f"duplicate label id at {path}:{line_number}: {label.id}")
        seen.add(label.id)
        labels.append(label)
    return labels


def evaluate_topic_labels(
    labels: list[TopicLabel],
    classifier: RuleTopicClassifier,
    *,
    minimum_samples: int = 100,
    threshold: float = 0.85,
    qwen: QwenClient | None = None,
) -> dict[str, Any]:
    """Compute Top-1 precision and refuse to pass undersized review sets."""

    correct = 0
    precision_correct = 0
    predicted_count = 0
    qwen_attempts = 0
    qwen_failures = 0
    errors: list[dict[str, str | None]] = []
    confusion: Counter[tuple[str, str]] = Counter()
    for label in labels:
        match = classifier.classify(label.title, label.text, event_type=label.event_type)
        final_topics = match.topics
        if qwen is not None and qwen.enabled and match.topics:
            qwen_attempts += 1
            enhanced = qwen.enhance(
                CollectedItem(
                    source_id="acceptance-eval",
                    external_id=label.id,
                    canonical_url=f"https://example.invalid/eval/{label.id}",
                    title=label.title,
                    summary=label.text,
                ),
                match.topics,
                summarize=False,
            )
            if enhanced is not None and enhanced.topics:
                final_topics = enhanced.topics
            elif enhanced is None:
                qwen_failures += 1
        predicted = (
            max(
                final_topics,
                key=lambda topic: match.strengths.get(topic.value, 0),
            )
            if final_topics
            else None
        )
        predicted_value = predicted.value if predicted is not None else None
        if predicted is not None:
            predicted_count += 1
            if predicted == label.expected_top1:
                precision_correct += 1
        if predicted == label.expected_top1:
            correct += 1
        else:
            errors.append(
                {
                    "id": label.id,
                    "expected": (
                        label.expected_top1.value
                        if label.expected_top1 is not None
                        else None
                    ),
                    "predicted": predicted_value,
                }
            )
        confusion[
            (
                label.expected_top1.value
                if label.expected_top1 is not None
                else "unclassified",
                predicted_value or "unclassified",
            )
        ] += 1

    total = len(labels)
    precision = precision_correct / predicted_count if predicted_count else 0.0
    accuracy = correct / total if total else 0.0
    enough_samples = total >= minimum_samples
    return {
        "samples": total,
        "minimum_samples": minimum_samples,
        "correct": correct,
        "predicted_samples": predicted_count,
        "top1_precision": round(precision, 6),
        "top1_accuracy": round(accuracy, 6),
        "threshold": threshold,
        "mode": "qwen_flash" if qwen is not None and qwen.enabled else "rules_only",
        "production_model_evaluated": bool(qwen is not None and qwen.enabled),
        "enough_samples": enough_samples,
        "qwen_attempts": qwen_attempts,
        "qwen_failures": qwen_failures,
        "passed": enough_samples and precision >= threshold and qwen_failures == 0,
        "confusion": [
            {"expected": expected, "predicted": predicted, "count": count}
            for (expected, predicted), count in sorted(confusion.items())
        ],
        "errors": errors,
    }
