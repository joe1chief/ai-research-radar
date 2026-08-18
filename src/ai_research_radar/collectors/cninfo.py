"""CNInfo statutory announcement collector for Shenzhen-listed issuers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..contracts import CollectedItem, CollectionBatch
from ..identity import normalize_content
from .base import BaseCollector, CollectorHTTPError
from .parsing import parse_datetime


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATIC_PDF_ORIGIN = "https://static.cninfo.com.cn"
DEFAULT_REFERER = "https://www.cninfo.com.cn/new/disclosure/stock"


class CNInfoAnnouncementsCollector(BaseCollector):
    """Read the official CNInfo announcement query API with a bounded overlap window."""

    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        cursor = dict(cursor or {})
        if not str(getattr(self.spec, "stock", "")).strip():
            raise CollectorHTTPError(f"CNInfo stock identifier is missing for {self.spec.id}")
        page_size = min(max(int(getattr(self.spec, "page_size", 30)), 1), 30)
        max_pages = max(1, int(getattr(self.spec, "max_pages", 4)))
        start_date, end_date = _date_window(cursor, self.spec)
        items: dict[str, CollectedItem] = {}
        warnings: list[str] = []
        more_pages = False

        for page_number in range(1, max_pages + 1):
            response = self.request(
                {},
                method="POST",
                data=_query_form(self.spec, page_number, page_size, start_date, end_date),
                extra_headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.cninfo.com.cn",
                    "Referer": str(getattr(self.spec, "referer", DEFAULT_REFERER)),
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise CollectorHTTPError(f"invalid CNInfo JSON for {self.spec.id}") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("announcements"), list):
                raise CollectorHTTPError(f"unexpected CNInfo response for {self.spec.id}")

            announcements = payload["announcements"]
            for announcement in announcements:
                item, warning = _announcement_item(announcement, self.spec)
                if warning:
                    warnings.append(warning)
                if item is not None:
                    items[item.external_id] = item

            more_pages = bool(payload.get("hasMore")) and len(announcements) >= page_size
            if not more_pages:
                break

        if more_pages:
            warnings.append(
                f"CNInfo page budget reached after {max_pages} pages; overlap window will replay"
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
        cursor["window_end"] = end_date.isoformat()
        return CollectionBatch(
            items=ordered,
            cursor=cursor,
            warnings=list(dict.fromkeys(warnings))[:10],
        )


def _query_form(spec, page_number: int, page_size: int, start_date, end_date) -> dict[str, str]:
    return {
        "pageNum": str(page_number),
        "pageSize": str(page_size),
        "column": str(getattr(spec, "column", "szse")),
        "tabName": "fulltext",
        "plate": str(getattr(spec, "plate", "sz")),
        "stock": str(getattr(spec, "stock")),
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }


def _date_window(cursor: dict[str, Any], spec, *, now: datetime | None = None):
    current = (now or datetime.now(tz=SHANGHAI)).astimezone(SHANGHAI)
    lookback_days = max(1, int(getattr(spec, "lookback_days", 45)))
    overlap_days = max(1, int(getattr(spec, "overlap_days", 7)))
    earliest = current - timedelta(days=lookback_days)
    watermark = parse_datetime(cursor.get("last_seen_at"))
    start = (
        max(
            earliest,
            min(current, watermark.astimezone(SHANGHAI)) - timedelta(days=overlap_days),
        )
        if watermark
        else earliest
    )
    return start.date(), current.date()


def _announcement_item(raw: Any, spec) -> tuple[CollectedItem | None, str | None]:
    if not isinstance(raw, dict):
        return None, "CNInfo returned a non-object announcement"
    announcement_id = str(raw.get("announcementId") or "").strip()
    title = normalize_content(
        re.sub(r"</?em\b[^>]*>", "", str(raw.get("announcementTitle") or ""), flags=re.I)
    )
    pdf_url = _static_pdf_url(raw.get("adjunctUrl"))
    if not announcement_id or not title or pdf_url is None:
        return None, f"CNInfo skipped malformed announcement {announcement_id or '<missing-id>'}"

    published_at = _millis(raw.get("announcementTime"))
    security_name = normalize_content(str(raw.get("secName") or spec.entity_id))
    ticker = str(raw.get("secCode") or getattr(spec, "ticker", "")).strip()
    announcement_type = normalize_content(str(raw.get("announcementTypeName") or ""))
    routine_patterns = list(getattr(spec, "routine_title_patterns", []))
    routine = any(re.search(pattern, title, re.I) for pattern in routine_patterns)
    display_title = f"{security_name} · {title}" if security_name else title

    return (
        CollectedItem(
            source_id=spec.id,
            external_id=announcement_id,
            canonical_url=pdf_url,
            title=display_title,
            summary=announcement_type,
            published_at=published_at,
            entity_id=spec.entity_id,
            evidence_type=spec.evidence_type,
            metadata={
                "announcement_id": announcement_id,
                "document_id": announcement_id,
                "exchange": "SZSE",
                "ticker": ticker,
                "org_id": str(raw.get("orgId") or getattr(spec, "org_id", "")),
                "announcement_type": announcement_type or None,
                "disclosure_date": published_at.astimezone(SHANGHAI).date().isoformat()
                if published_at
                else None,
                "routine": routine,
            },
        ),
        None,
    )


def _static_pdf_url(value: Any) -> str | None:
    path = str(value or "").strip().replace("\\", "/")
    parts = urlsplit(path)
    if not path or parts.scheme or parts.netloc or path.startswith("//"):
        return None
    clean_path = "/" + parts.path.lstrip("/")
    if not clean_path.casefold().startswith("/finalpage/") or not clean_path.casefold().endswith(
        ".pdf"
    ):
        return None
    return f"{STATIC_PDF_ORIGIN}{clean_path}"


def _millis(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
