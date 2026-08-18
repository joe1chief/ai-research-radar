"""Hugging Face models API collector."""

from __future__ import annotations

import httpx

from ..contracts import CollectedItem, CollectionBatch
from .base import BaseCollector, CollectorHTTPError
from .parsing import parse_datetime


class HuggingFaceModelsCollector(BaseCollector):
    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        payload = response.json()
        warnings: list[str] = []
        if not isinstance(payload, list):
            return CollectionBatch(
                cursor=next_cursor,
                warnings=["Hugging Face API payload was not a list"],
            )
        items: list[CollectedItem] = []
        repo_type = str(getattr(self.spec, "repo_type", "model"))
        for model in payload:
            model_id = str(model.get("modelId") or model.get("id") or "")
            if not model_id:
                continue
            items.append(
                CollectedItem(
                    source_id=self.spec.id,
                    external_id=model_id,
                    canonical_url=(
                        f"https://huggingface.co/datasets/{model_id}"
                        if repo_type == "dataset"
                        else f"https://huggingface.co/{model_id}"
                    ),
                    title=model_id,
                    summary=str(model.get("pipeline_tag") or ""),
                    updated_at=parse_datetime(model.get("lastModified")),
                    entity_id=self.spec.entity_id,
                    evidence_type=self.spec.evidence_type,
                    metadata={
                        "downloads": model.get("downloads"),
                        "likes": model.get("likes"),
                        "tags": model.get("tags", []),
                        "sha": model.get("sha"),
                        "repo_type": repo_type,
                    },
                )
            )
        detail_limit = max(0, int(getattr(self.spec, "detail_fetch_limit", 0)))
        for item in items[:detail_limit]:
            sha = str(item.metadata.get("sha") or "main")
            raw_url = f"{item.canonical_url}/raw/{sha}/README.md"
            try:
                detail = self.request({}, url=raw_url)
                if len(detail.content) <= 512_000:
                    item.content = detail.text[:100_000]
                    item.metadata["model_card_url"] = raw_url
            except (CollectorHTTPError, httpx.HTTPError, ValueError) as exc:
                warnings.append(f"model card unavailable for {item.external_id}: {exc}")
        return CollectionBatch(items=items, cursor=next_cursor, warnings=warnings[:10])
