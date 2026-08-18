"""Small dependency-free parsing helpers."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import struct_time


def parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, struct_time):
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (TypeError, ValueError):
            return None
