"""YAML configuration loading with defaults and anchor support."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .contracts import SourceSpec


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"expected mapping at {path}")
    return data


def load_sources(config_dir: Path | str = "configs") -> list[SourceSpec]:
    document = load_yaml(Path(config_dir) / "sources.yml")
    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("sources.yml defaults must be a mapping")
    result: list[SourceSpec] = []
    for raw in document.get("sources", []):
        merged = {**defaults, **raw}
        result.append(SourceSpec.model_validate(merged))
    return result


def load_topics(config_dir: Path | str = "configs") -> dict[str, Any]:
    return load_yaml(Path(config_dir) / "topics.yml")


def load_issuers(config_dir: Path | str = "configs") -> list[dict[str, Any]]:
    return load_yaml(Path(config_dir) / "issuers.yml").get("issuers", [])
