"""Incremental arXiv OAI-PMH reconciliation using arXivRaw version history."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET

from ..contracts import CollectedItem, CollectionBatch
from ..identity import canonicalize_url
from ..db import utcnow
from .base import BaseCollector
from .parsing import parse_datetime


DEFAULT_SETS = ["cs:cs:AI", "cs:cs:CL", "cs:cs:LG", "cs:cs:MA", "cs:cs:SE", "cs:cs:RO", "cs:cs:CR"]
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
RAW_NS = "{http://arxiv.org/OAI/arXivRaw/}"


class ArxivOAICollector(BaseCollector):
    def collect(self, cursor: dict[str, Any] | None = None) -> CollectionBatch:
        cursor = dict(cursor or {})
        sets = list(getattr(self.spec, "sets", DEFAULT_SETS))
        max_pages = max(int(getattr(self.spec, "max_pages", 20)), len(sets))
        interval = float(getattr(self.spec, "request_interval_seconds", 0))
        from_date = str(cursor.get("oai_from") or (utcnow() - timedelta(days=3)).date())
        set_index = min(int(cursor.get("set_index", 0)), max(len(sets) - 1, 0))
        token = str(cursor.get("resumption_token") or "")
        response_date = from_date
        items: dict[str, CollectedItem] = {}
        warnings: list[str] = []

        for page in range(max_pages):
            if not sets:
                break
            params = (
                {"verb": "ListRecords", "resumptionToken": token}
                if token
                else {
                    "verb": "ListRecords",
                    "metadataPrefix": "arXivRaw",
                    "from": from_date,
                    "set": sets[set_index],
                }
            )
            response = self.request({}, params=params)
            root = ET.fromstring(response.content)
            response_date = (root.findtext(f"{OAI_NS}responseDate") or response_date)[:10]
            error = root.find(f"{OAI_NS}error")
            if error is not None and error.get("code") != "noRecordsMatch":
                warnings.append(f"OAI {error.get('code', 'error')}: {(error.text or '').strip()}")
            for record in root.findall(f".//{OAI_NS}record"):
                item = _record_item(record, self.spec)
                if item is None:
                    continue
                previous = items.get(item.external_id)
                if previous is None or int(item.metadata["version"]) > int(previous.metadata["version"]):
                    items[item.external_id] = item
            token = (root.findtext(f".//{OAI_NS}resumptionToken") or "").strip()
            if not token:
                set_index += 1
                if set_index >= len(sets):
                    return CollectionBatch(
                        items=list(items.values()),
                        cursor={
                            "oai_from": response_date,
                            "set_index": 0,
                            "resumption_token": "",
                        },
                        warnings=warnings,
                    )
            if interval and page + 1 < max_pages:
                self.sleep(interval)

        return CollectionBatch(
            items=list(items.values()),
            cursor={
                "oai_from": from_date,
                "set_index": set_index,
                "resumption_token": token,
            },
            warnings=[*warnings, "OAI reconciliation page budget reached; cursor will resume"],
        )


def _record_item(record: ET.Element, spec) -> CollectedItem | None:
    header = record.find(f"{OAI_NS}header")
    if header is None or header.get("status") == "deleted":
        return None
    raw = record.find(f".//{RAW_NS}arXivRaw")
    if raw is None:
        return None
    arxiv_id = (raw.findtext(f"{RAW_NS}id") or "").strip()
    versions = raw.findall(f"{RAW_NS}version")
    if not arxiv_id or not versions:
        return None
    latest = versions[-1]
    version_text = latest.get("version") or f"v{len(versions)}"
    match = re.search(r"(\d+)", version_text)
    version = int(match.group(1)) if match else len(versions)
    history = [
        {
            "version": value.get("version"),
            "date": value.findtext(f"{RAW_NS}date"),
            "size": value.findtext(f"{RAW_NS}size"),
        }
        for value in versions
    ]
    text = "\n".join(
        filter(
            None,
            [
                raw.findtext(f"{RAW_NS}abstract"),
                raw.findtext(f"{RAW_NS}comments"),
                raw.findtext(f"{RAW_NS}journal-ref"),
            ],
        )
    )
    code_match = re.search(r"https?://(?:www\.)?(?:github\.com|gitlab\.com)/[^\s<>]+", text)
    authors_text = (raw.findtext(f"{RAW_NS}authors") or "").strip()
    authors = [name.strip() for name in re.split(r",|\band\b", authors_text) if name.strip()]
    return CollectedItem(
        source_id=spec.id,
        external_id=arxiv_id,
        canonical_url=canonicalize_url(f"https://arxiv.org/abs/{arxiv_id}v{version}"),
        title=(raw.findtext(f"{RAW_NS}title") or "").strip(),
        summary=(raw.findtext(f"{RAW_NS}abstract") or "").strip(),
        authors=authors,
        published_at=parse_datetime(versions[0].findtext(f"{RAW_NS}date")),
        updated_at=parse_datetime(latest.findtext(f"{RAW_NS}date")),
        entity_id=spec.entity_id,
        evidence_type=spec.evidence_type,
        metadata={
            "arxiv_id": arxiv_id,
            "version": version,
            "version_history": history,
            "categories": (raw.findtext(f"{RAW_NS}categories") or "").split(),
            "authors": authors,
            "comments": raw.findtext(f"{RAW_NS}comments"),
            "journal_reference": raw.findtext(f"{RAW_NS}journal-ref"),
            "doi": raw.findtext(f"{RAW_NS}doi"),
            "oai_datestamp": header.findtext(f"{OAI_NS}datestamp"),
            "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}v{version}",
            "alphaxiv_url": f"https://alphaxiv.org/abs/{arxiv_id}",
            "code_url": code_match.group(0).rstrip(".,);]") if code_match else None,
        },
    )
