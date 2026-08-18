"""Public OpenReview API v2 accepted/camera-ready paper collector."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..contracts import CollectedItem, CollectionBatch
from ..identity import normalize_content
from .base import BaseCollector
from .conference import _relevant


class OpenReviewCollector(BaseCollector):
    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        venue_ids = list(getattr(self.spec, "venue_ids", []))
        limit = min(int(getattr(self.spec, "page_size", 1000)), 1000)
        max_pages = max(1, int(getattr(self.spec, "max_pages", 5)))
        items: dict[str, CollectedItem] = {}
        warnings: list[str] = []
        for venue_id in venue_ids:
            try:
                for page in range(max_pages):
                    response = self.request(
                        {},
                        params={
                            "content.venueid": venue_id,
                            "limit": limit,
                            "offset": page * limit,
                            "sort": "tmdate:desc",
                        },
                    )
                    payload = response.json()
                    notes = payload.get("notes", []) if isinstance(payload, dict) else []
                    for note in notes:
                        content = note.get("content") or {}
                        title = normalize_content(str(_value(content.get("title")) or ""))
                        abstract = normalize_content(str(_value(content.get("abstract")) or ""))
                        if not title or not _relevant(title, abstract):
                            continue
                        note_id = str(note.get("id") or "")
                        if not note_id:
                            continue
                        authors = _value(content.get("authors")) or []
                        if not isinstance(authors, list):
                            authors = [authors]
                        invitations = note.get("invitations") or []
                        items[note_id] = CollectedItem(
                            source_id=self.spec.id,
                            external_id=note_id,
                            canonical_url=f"https://openreview.net/forum?id={note_id}",
                            title=title,
                            summary=abstract,
                            authors=[str(author) for author in authors],
                            published_at=_millis(note.get("pdate") or note.get("odate")),
                            updated_at=_millis(note.get("mdate") or note.get("tmdate")),
                            entity_id=self.spec.entity_id,
                            evidence_type=self.spec.evidence_type,
                            metadata={
                                "venue_id": venue_id,
                                "venue": _value(content.get("venue")),
                                "acceptance_status": "camera_ready"
                                if any("Camera_Ready" in str(value) for value in invitations)
                                else "accepted",
                                "revision": note.get("mdate") or note.get("tmdate"),
                            },
                        )
                    if len(notes) < limit:
                        break
            except Exception as exc:
                warnings.append(f"OpenReview venue {venue_id} failed: {exc}")
        return CollectionBatch(items=list(items.values()), cursor=dict(cursor or {}), warnings=warnings[:10])


def _value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _millis(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
