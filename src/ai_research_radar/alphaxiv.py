"""Optional alphaXiv MCP deep-reading with a strict, failure-isolated boundary.

arXiv remains the canonical source. alphaXiv enriches at most the configured
Top-N paper cards and never blocks collection, classification, or delivery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import (
    EventItemModel,
    EventRevisionModel,
    ItemVersionModel,
    RadarEventModel,
    reserve_daily_usage,
    utcnow,
)
from .identity import normalize_content


@dataclass(frozen=True, slots=True)
class AlphaXivInsight:
    arxiv_id: str
    summary: str
    key_findings: list[str]


class AlphaXivAdapter(Protocol):
    def deep_read(self, arxiv_id: str) -> AlphaXivInsight | None: ...


class DisabledAlphaXivAdapter:
    """Safe degradation used when OAuth/token or an MCP transport is unavailable."""

    def __init__(self, reason: str = "alphaXiv MCP is not configured") -> None:
        self.reason = reason

    def deep_read(self, arxiv_id: str) -> None:
        return None


class MCPAlphaXivAdapter:
    """Minimal Streamable-HTTP MCP client for alphaXiv's read-only research tool."""

    protocol_version = "2025-06-18"

    def __init__(
        self,
        *,
        access_token: str,
        endpoint: str = "https://api.alphaxiv.org/mcp/v1",
        client: httpx.Client | None = None,
    ) -> None:
        self.access_token = access_token
        self.endpoint = endpoint
        self.client = client or httpx.Client(timeout=90)
        self._owns_client = client is None
        self._session_id: str | None = None
        self._request_id = 0

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def deep_read(self, arxiv_id: str) -> AlphaXivInsight | None:
        try:
            self._ensure_session()
            result = self._rpc(
                "tools/call",
                {
                    "name": "get_paper_content",
                    "arguments": {
                        "url": f"https://arxiv.org/abs/{arxiv_id}",
                        "fullText": False,
                    },
                },
            )
            if not isinstance(result, dict) or result.get("isError"):
                return None
            texts = [
                str(block.get("text", ""))
                for block in result.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            report = normalize_content("\n".join(texts))[:6000]
            if not report:
                return None
            findings = [
                line.strip(" -•\t")[:500]
                for line in "\n".join(texts).splitlines()
                if line.strip().startswith(("-", "•"))
            ][:5]
            return AlphaXivInsight(arxiv_id=arxiv_id, summary=report, key_findings=findings)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "ai-research-radar", "version": "0.1.0"},
            },
        )
        self._notification("notifications/initialized", {})

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        response = self.client.post(
            self.endpoint,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
        )
        response.raise_for_status()
        if response.headers.get("Mcp-Session-Id"):
            self._session_id = response.headers["Mcp-Session-Id"]
        payload = _mcp_payload(response)
        if payload.get("error"):
            raise ValueError(str(payload["error"]))
        return payload.get("result")

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        response = self.client.post(
            self.endpoint,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": method, "params": params},
        )
        response.raise_for_status()


def _mcp_payload(response: httpx.Response) -> dict[str, Any]:
    if "text/event-stream" in response.headers.get("content-type", ""):
        data_lines = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        if not data_lines:
            raise ValueError("MCP event stream contained no data")
        payload = json.loads(data_lines[-1])
    else:
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MCP response must be a JSON object")
    return payload


def enrich_alphaxiv_top(
    session: Session,
    adapter: AlphaXivAdapter,
    *,
    limit: int = 5,
    timezone: str = "Asia/Shanghai",
    daily_limit: int = 5,
) -> dict[str, int]:
    """Deep-read the highest-scoring not-yet-enriched paper events."""

    stats = {"attempted": 0, "enriched": 0, "failed": 0}
    threshold = utcnow() - timedelta(hours=36)
    events = session.scalars(
        select(RadarEventModel)
        .where(
            RadarEventModel.event_type == "PAPER",
            or_(
                RadarEventModel.first_seen_at >= threshold,
                RadarEventModel.material_updated_at >= threshold,
            ),
        )
        .order_by(RadarEventModel.score.desc(), RadarEventModel.first_seen_at.desc())
        .limit(max(limit * 20, 100))
    ).all()
    usage_date = utcnow().astimezone(ZoneInfo(timezone)).date()
    for event in events:
        if stats["attempted"] >= limit:
            break
        versions = session.scalars(
            select(ItemVersionModel)
            .join(EventItemModel, EventItemModel.item_version_id == ItemVersionModel.id)
            .where(EventItemModel.event_id == event.id)
            .order_by(ItemVersionModel.fetched_at.desc())
        ).all()
        version = next(
            (
                candidate
                for candidate in versions
                if (candidate.metadata_json or {}).get("arxiv_id")
            ),
            None,
        )
        if version is None:
            continue
        metadata = dict(version.metadata_json or {})
        if metadata.get("alphaxiv_enriched_at"):
            continue
        attempted_at = str(metadata.get("alphaxiv_attempted_at") or "")
        if attempted_at.startswith(usage_date.isoformat()):
            continue
        arxiv_id = str(metadata.get("arxiv_id") or "")
        if not arxiv_id:
            continue
        if not reserve_daily_usage(
            session,
            usage_date=usage_date,
            usage_key="alphaxiv_deep_read",
            hard_limit=daily_limit,
        ):
            break
        stats["attempted"] += 1
        metadata["alphaxiv_attempted_at"] = utcnow().isoformat().replace("+00:00", "Z")
        version.metadata_json = metadata
        insight = adapter.deep_read(arxiv_id)
        if insight is None:
            stats["failed"] += 1
            continue
        metadata["alphaxiv_insight"] = {
            "summary": insight.summary[:6000],
            "key_findings": insight.key_findings[:5],
        }
        enriched_at = utcnow()
        if enriched_at.tzinfo is None:
            enriched_at = enriched_at.replace(tzinfo=UTC)
        metadata["alphaxiv_enriched_at"] = enriched_at.isoformat().replace("+00:00", "Z")
        version.metadata_json = metadata
        revision = session.scalar(
            select(EventRevisionModel)
            .where(
                EventRevisionModel.event_id == event.id,
                EventRevisionModel.is_material.is_(True),
            )
            .order_by(EventRevisionModel.revision_no.desc())
        )
        if revision is not None:
            revision.snapshot = {
                **(revision.snapshot or {}),
                "alphaxiv_insight": metadata["alphaxiv_insight"],
            }
        stats["enriched"] += 1
    session.flush()
    return stats


def alphaxiv_url(arxiv_id: str) -> str:
    return f"https://alphaxiv.org/abs/{arxiv_id}"
