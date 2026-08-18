"""SEC submissions collector for material filings and ownership disclosures."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

import httpx

from ..contracts import CollectedItem, CollectionBatch
from ..identity import normalize_content
from .base import BaseCollector, CollectorHTTPError
from .parsing import parse_datetime


TRACKED_FORMS = {
    "8-K",
    "8-K/A",
    "10-Q",
    "10-Q/A",
    "10-K",
    "10-K/A",
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
    "6-K",
    "20-F",
    "20-F/A",
    "4",
    "4/A",
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",
}


class SECSubmissionsCollector(BaseCollector):
    def __init__(
        self,
        *args,
        now: Callable[[], datetime] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._now = now or (lambda: datetime.now(tz=UTC))

    def collect(self, cursor: dict | None = None) -> CollectionBatch:
        cursor = dict(cursor or {})
        response = self.request(cursor)
        next_cursor = self.next_cursor(response, cursor)
        if response.status_code == 304:
            return CollectionBatch(cursor=next_cursor, not_modified=True)
        payload = response.json()
        recent = (payload.get("filings") or {}).get("recent") or {}
        accessions = recent.get("accessionNumber") or []
        cik = str(payload.get("cik") or _cik_from_url(self.spec.url)).lstrip("0")
        company = str(payload.get("name") or self.spec.entity_id)
        last_seen_native_id = str(cursor.get("last_seen_native_id") or "").strip()
        cutoff_date = _lookback_cutoff(
            self._now(),
            int(getattr(self.spec, "lookback_days", 45)),
        )
        cursor_found = False
        items: list[CollectedItem] = []
        warnings: list[str] = []
        for index, accession in enumerate(accessions):
            accession = str(accession or "").strip()
            if last_seen_native_id and accession == last_seen_native_id:
                cursor_found = True
                break

            filing_date = _at(recent, "filingDate", index)
            parsed_filing_date = _date_value(filing_date)
            # SEC's ``filings.recent`` arrays are newest-first. The date
            # boundary protects first runs (and a missing/expired cursor) from
            # replaying years of history.
            if parsed_filing_date is not None and parsed_filing_date < cutoff_date:
                break

            form = str(_at(recent, "form", index) or "")
            if not _tracked_form(form):
                continue
            primary_document = str(_at(recent, "primaryDocument", index) or "")
            accession_compact = str(accession).replace("-", "")
            filing_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_compact}/{primary_document}"
                if primary_document
                else f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_compact}"
            )
            description = str(_at(recent, "primaryDocDescription", index) or "")
            filing_items = str(_at(recent, "items", index) or "")
            items.append(
                CollectedItem(
                    source_id=self.spec.id,
                    external_id=str(accession),
                    canonical_url=filing_url,
                    title=f"{company} · {form} · {filing_date}",
                    summary=" · ".join(filter(None, [description, filing_items])),
                    published_at=parse_datetime(filing_date),
                    updated_at=parse_datetime(_at(recent, "acceptanceDateTime", index)),
                    entity_id=self.spec.entity_id,
                    evidence_type=self.spec.evidence_type,
                    metadata={
                        "accession_number": accession,
                        "form": form,
                        "filing_date": filing_date,
                        "report_date": _at(recent, "reportDate", index),
                        "primary_document": primary_document,
                        "filing_items": filing_items,
                        "routine": form in {"4", "4/A", "SC 13G", "SC 13G/A"},
                    },
                )
            )
        if last_seen_native_id and not cursor_found:
            warnings.append(
                "SEC cursor was not present in filings.recent; "
                f"used the {max(1, int(getattr(self.spec, 'lookback_days', 45)))}-day "
                "safety window"
            )

        items.sort(
            key=lambda item: (
                item.updated_at
                or item.published_at
                or datetime.min.replace(tzinfo=UTC),
                item.external_id,
            ),
            reverse=True,
        )
        detail_limit = max(0, int(getattr(self.spec, "filing_detail_limit", 5)))
        detailed = 0
        for item in items:
            if item.metadata.get("routine"):
                continue
            if detailed >= detail_limit:
                break
            detailed += 1
            try:
                detail = self.request({}, url=item.canonical_url)
                if "html" not in detail.headers.get("content-type", ""):
                    continue
                parser = _FilingTextParser()
                parser.feed(detail.text)
                item.content = normalize_content(" ".join(parser.parts))[:100000]
            except (CollectorHTTPError, httpx.HTTPError) as exc:
                warnings.append(f"SEC filing detail unavailable for {item.external_id}: {exc}")
        newest_accession = next(
            (str(accession).strip() for accession in accessions if str(accession).strip()),
            "",
        )
        if newest_accession:
            next_cursor["last_seen_native_id"] = newest_accession
        return CollectionBatch(items=items, cursor=next_cursor, warnings=warnings[:10])


class _FilingTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1
        elif not self._ignored and tag in {"p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


def _at(mapping: dict, key: str, index: int):
    values = mapping.get(key) or []
    return values[index] if index < len(values) else None


def _tracked_form(form: str) -> bool:
    return form in TRACKED_FORMS or form.startswith("424B")


def _cik_from_url(url: str) -> str:
    match = re.search(r"CIK(\d+)\.json", url, re.I)
    return match.group(1) if match else ""


def _lookback_cutoff(now: datetime, lookback_days: int) -> date:
    lookback_days = max(1, min(lookback_days, 3660))
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).date() - timedelta(days=lookback_days - 1)


def _date_value(value: object) -> date | None:
    parsed = parse_datetime(value)
    return parsed.astimezone(UTC).date() if parsed is not None else None
