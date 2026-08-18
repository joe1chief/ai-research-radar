"""Shanghai Stock Exchange company-announcement collector.

The SSE company bulletin endpoint is the primary structured source for listed
company announcements. It requires the public announcement page as Referer and
uses ``pageHelp.beginPage`` as part of its page selector in addition to
``pageHelp.pageNo``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from ..contracts import CollectedItem, CollectionBatch
from ..identity import normalize_content
from .base import BaseCollector, CollectorHTTPError


SSE_PUBLIC_ORIGIN = "https://www.sse.com.cn"
SSE_ANNOUNCEMENT_REFERER = (
    "https://www.sse.com.cn/assortment/stock/list/info/announcement/"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_SECURITY_TYPES = "0101,120100,020100,020200,120200"


class SSEAnnouncementsCollector(BaseCollector):
    """Collect a rolling, paginated date window of official SSE PDFs."""

    def __init__(
        self,
        *args,
        now: Callable[[], datetime] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._now = now or (lambda: datetime.now(tz=SHANGHAI))

    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        product_id = normalize_content(str(getattr(self.spec, "product_id", "")))
        if not re.fullmatch(r"\d{6}", product_id):
            raise CollectorHTTPError(
                f"invalid SSE product_id for {self.spec.id}: expected six digits"
            )

        lookback_days = max(1, min(int(getattr(self.spec, "lookback_days", 45)), 366))
        page_size = max(1, min(int(getattr(self.spec, "page_size", 50)), 100))
        max_pages = max(1, min(int(getattr(self.spec, "max_pages", 10)), 100))
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=SHANGHAI)
        local_date = now.astimezone(SHANGHAI).date()
        begin_date = local_date - timedelta(days=lookback_days - 1)

        items: list[CollectedItem] = []
        seen: set[str] = set()
        warnings: list[str] = []
        page_count = 1
        total = 0
        next_cursor = dict(cursor or {})

        for page_no in range(1, max_pages + 1):
            response = self.request(
                cursor if page_no == 1 else {},
                params=self._params(
                    product_id=product_id,
                    begin_date=begin_date.isoformat(),
                    end_date=local_date.isoformat(),
                    page_size=page_size,
                    page_no=page_no,
                ),
                extra_headers={
                    "Referer": SSE_ANNOUNCEMENT_REFERER,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if page_no == 1:
                next_cursor = self.next_cursor(response, cursor)
                if response.status_code == 304:
                    return CollectionBatch(cursor=next_cursor, not_modified=True)

            payload = _json_payload(response.content, source_id=self.spec.id)
            page_help = payload.get("pageHelp") or {}
            if not isinstance(page_help, dict):
                raise CollectorHTTPError(f"malformed SSE pageHelp for {self.spec.id}")
            if page_no == 1:
                page_count = _nonnegative_int(page_help.get("pageCount"), default=1)
                total = _nonnegative_int(page_help.get("total"), default=0)

            rows = payload.get("result")
            if rows is None:
                rows = page_help.get("data", [])
            if not isinstance(rows, list):
                raise CollectorHTTPError(f"malformed SSE result list for {self.spec.id}")

            skipped = 0
            for row in rows:
                item = self._item(row, expected_product_id=product_id)
                if item is None:
                    skipped += 1
                    continue
                if item.external_id in seen:
                    continue
                seen.add(item.external_id)
                items.append(item)
            if skipped:
                warnings.append(f"SSE page {page_no}: skipped {skipped} malformed rows")

            if page_no >= page_count or not rows:
                break

        if page_count > max_pages:
            warnings.append(
                f"SSE result truncated at {max_pages} of {page_count} pages; "
                "increase max_pages or shorten the date window"
            )

        next_cursor.update(
            {
                "window_start": begin_date.isoformat(),
                "window_end": local_date.isoformat(),
                "last_polled_at": now.astimezone(UTC).isoformat(),
                "page_count": page_count,
                "total": total,
            }
        )
        if items:
            next_cursor["last_seen_native_id"] = items[0].external_id
        return CollectionBatch(items=items, cursor=next_cursor, warnings=warnings[:10])

    def _params(
        self,
        *,
        product_id: str,
        begin_date: str,
        end_date: str,
        page_size: int,
        page_no: int,
    ) -> dict[str, str]:
        return {
            "isPagination": "true",
            "productId": product_id,
            "keyWord": str(getattr(self.spec, "keyword", "")),
            "securityType": str(
                getattr(self.spec, "security_types", DEFAULT_SECURITY_TYPES)
            ),
            "reportType2": "",
            "reportType": str(getattr(self.spec, "report_type", "ALL")),
            "beginDate": begin_date,
            "endDate": end_date,
            "pageHelp.pageSize": str(page_size),
            "pageHelp.pageNo": str(page_no),
            # The endpoint repeats page one if beginPage is left at one.
            "pageHelp.beginPage": str(page_no),
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": str(page_no),
        }

    def _item(self, row: object, *, expected_product_id: str) -> CollectedItem | None:
        if not isinstance(row, dict):
            return None
        security_code = normalize_content(str(row.get("SECURITY_CODE") or ""))
        if security_code and security_code != expected_product_id:
            return None
        security_code = security_code or expected_product_id

        canonical_url = _canonical_sse_pdf_url(str(row.get("URL") or ""))
        title = normalize_content(str(row.get("TITLE") or ""))
        if canonical_url is None or not title:
            return None
        document_id = PurePosixPath(urlsplit(canonical_url).path).stem
        if not document_id:
            return None

        security_name = normalize_content(str(row.get("SECURITY_NAME") or ""))
        bulletin_heading = normalize_content(str(row.get("BULLETIN_HEADING") or ""))
        bulletin_type = normalize_content(str(row.get("BULLETIN_TYPE") or ""))
        summary = " · ".join(dict.fromkeys(filter(None, [bulletin_heading, bulletin_type])))
        routine_patterns = list(getattr(self.spec, "routine_title_patterns", []))

        return CollectedItem(
            source_id=self.spec.id,
            external_id=document_id,
            canonical_url=canonical_url,
            title=f"{security_name or self.spec.entity_id}（{security_code}） · {title}",
            summary=summary,
            published_at=_parse_sse_datetime(row.get("SSEDATE")),
            updated_at=_parse_sse_datetime(row.get("ADDDATE")),
            entity_id=self.spec.entity_id,
            evidence_type=self.spec.evidence_type,
            metadata={
                "document_id": document_id,
                "exchange": "SSE",
                "ticker": security_code,
                "security_name": security_name,
                "bulletin_heading": bulletin_heading,
                "bulletin_type": bulletin_type,
                "filing_date": normalize_content(str(row.get("SSEDATE") or "")),
                "added_at": normalize_content(str(row.get("ADDDATE") or "")),
                "routine": any(
                    re.search(pattern, title, flags=re.IGNORECASE)
                    for pattern in routine_patterns
                ),
            },
        )


def _json_payload(raw: bytes, *, source_id: str) -> dict:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorHTTPError(f"invalid SSE JSON for {source_id}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectorHTTPError(f"malformed SSE payload for {source_id}")
    return payload


def _canonical_sse_pdf_url(raw_url: str) -> str | None:
    absolute = urljoin(f"{SSE_PUBLIC_ORIGIN}/", raw_url.strip())
    parts = urlsplit(absolute)
    host = (parts.hostname or "").casefold()
    decoded_path = unquote(parts.path)
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or host not in {"sse.com.cn", "www.sse.com.cn"}
        or not decoded_path.startswith("/disclosure/listedinfo/announcement/")
        or any(part == ".." for part in decoded_path.split("/"))
        or not decoded_path.casefold().endswith(".pdf")
    ):
        return None
    # SSE rows use relative paths. Pinning the public origin and dropping query
    # parameters makes mirrors of the same official document one stable URL.
    return f"{SSE_PUBLIC_ORIGIN}{parts.path}"


def _parse_sse_datetime(value: object) -> datetime | None:
    text = normalize_content(str(value or ""))
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(UTC)


def _nonnegative_int(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default
