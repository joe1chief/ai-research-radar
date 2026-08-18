"""Official Chinese national AI standards collector.

The SAMR landing page contains only navigation links.  The public national
standards catalogue exposes a structured, paginated search endpoint; this
collector queries that endpoint and turns each catalogue row into a stable
record backed by the official standard-detail page.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from ..contracts import CollectedItem, CollectionBatch
from ..identity import normalize_content
from .base import BaseCollector, CollectorHTTPError
from .parsing import parse_datetime


SAMR_REFERER = "https://std.samr.gov.cn/gb/gbQuery"
SAMR_DETAIL_URL = "https://std.samr.gov.cn/gb/search/gbDetailed?id={}"
DEFAULT_SEARCH_TERMS = ("人工智能", "大模型")
STANDARD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")


class SAMRStandardsCollector(BaseCollector):
    """Collect bounded keyword searches from SAMR's official JSON catalogue."""

    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        cursor = dict(cursor or {})
        terms = _search_terms(getattr(self.spec, "search_terms", DEFAULT_SEARCH_TERMS))
        if not terms:
            raise CollectorHTTPError(f"SAMR search_terms are missing for {self.spec.id}")

        page_size = min(max(int(getattr(self.spec, "page_size", 50)), 1), 50)
        max_pages = min(max(int(getattr(self.spec, "max_pages_per_term", 4)), 1), 20)
        items: dict[str, CollectedItem] = {}
        warnings: list[str] = []
        totals: dict[str, int] = {}

        for term in terms:
            total = 0
            exhausted = False
            for page_number in range(1, max_pages + 1):
                try:
                    response = self.request(
                        {},
                        params={
                            "searchText": term,
                            "ics": str(getattr(self.spec, "ics", "")),
                            "state": str(getattr(self.spec, "state", "")),
                            "ISSUE_DATE": str(getattr(self.spec, "issue_date", "")),
                            "pageNumber": str(page_number),
                            "pageSize": str(page_size),
                        },
                        extra_headers={
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "Referer": str(getattr(self.spec, "referer", SAMR_REFERER)),
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    payload = _json_payload(response, source_id=self.spec.id)
                except (CollectorHTTPError, ValueError) as exc:
                    warnings.append(f"SAMR query {term!r} page {page_number} failed: {exc}")
                    exhausted = True
                    break

                rows = payload.get("rows")
                if not isinstance(rows, list):
                    warnings.append(
                        f"SAMR query {term!r} page {page_number} returned malformed rows"
                    )
                    exhausted = True
                    break
                total = _nonnegative_int(payload.get("total"), default=len(rows))
                totals[term] = total

                skipped = 0
                for row in rows:
                    item = _standard_item(row, self.spec, matched_term=term)
                    if item is None:
                        skipped += 1
                        continue
                    existing = items.get(item.external_id)
                    if existing is None:
                        items[item.external_id] = item
                        continue
                    matched = list(existing.metadata.get("matched_search_terms") or [])
                    if term not in matched:
                        matched.append(term)
                    existing.metadata["matched_search_terms"] = matched
                if skipped:
                    warnings.append(
                        f"SAMR query {term!r} page {page_number}: "
                        f"skipped {skipped} malformed rows"
                    )

                if not rows or page_number * page_size >= total or len(rows) < page_size:
                    exhausted = True
                    break

            if not exhausted and total > max_pages * page_size:
                warnings.append(
                    f"SAMR query {term!r} page budget reached at {max_pages} pages "
                    f"for {total} records"
                )

        ordered = sorted(
            items.values(),
            key=lambda item: (
                item.published_at or datetime.min.replace(tzinfo=UTC),
                item.external_id,
            ),
            reverse=True,
        )
        if ordered and ordered[0].published_at is not None:
            cursor["last_seen_at"] = ordered[0].published_at.isoformat()
            cursor["last_seen_native_id"] = ordered[0].external_id
        cursor["search_totals"] = totals
        return CollectionBatch(
            items=ordered,
            cursor=cursor,
            warnings=list(dict.fromkeys(warnings))[:10],
        )


def _search_terms(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            term
            for value in raw
            if (term := normalize_content(str(value)))
        )
    )


def _json_payload(response, *, source_id: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CollectorHTTPError(f"invalid SAMR JSON for {source_id}") from exc
    if not isinstance(payload, dict):
        raise CollectorHTTPError(f"unexpected SAMR response for {source_id}")
    return payload


def _standard_item(row: object, spec, *, matched_term: str) -> CollectedItem | None:
    if not isinstance(row, dict):
        return None
    standard_id = normalize_content(str(row.get("id") or ""))
    if STANDARD_ID_PATTERN.fullmatch(standard_id) is None:
        return None

    name = _plain_text(row.get("C_C_NAME"))
    standard_code = _plain_text(row.get("C_STD_CODE"))
    if not name:
        return None
    issue_date = _plain_text(row.get("ISSUE_DATE"))
    effective_date = _plain_text(row.get("ACT_DATE"))
    nature = _plain_text(row.get("STD_NATURE"))
    state = _plain_text(row.get("STATE"))
    summary_parts = [
        f"性质：{nature}" if nature else "",
        f"状态：{state}" if state else "",
        f"发布日期：{issue_date}" if issue_date else "",
        f"实施日期：{effective_date}" if effective_date else "",
    ]
    return CollectedItem(
        source_id=spec.id,
        external_id=standard_id,
        canonical_url=SAMR_DETAIL_URL.format(quote(standard_id, safe="")),
        title=f"{standard_code} · {name}" if standard_code else name,
        summary="；".join(part for part in summary_parts if part),
        published_at=parse_datetime(issue_date),
        entity_id=spec.entity_id,
        evidence_type=spec.evidence_type,
        metadata={
            "standard_id": standard_id,
            "standard_code": standard_code,
            "standard_nature": nature,
            "state": state,
            "issue_date": issue_date,
            "effective_date": effective_date,
            "project_id": row.get("PROJECT_ID"),
            "matched_search_terms": [matched_term],
        },
    )


def _plain_text(value: object) -> str:
    without_markup = re.sub(r"<[^>]*>", "", html.unescape(str(value or "")))
    return normalize_content(without_markup)


def _nonnegative_int(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default
