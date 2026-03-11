from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime


def is_probably_pdf(first_bytes: bytes) -> bool:
    sniff = first_bytes[:1024].lstrip()
    return sniff.startswith(b"%PDF-")


def parse_retry_after_seconds(value: str) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(int(raw))
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        now = datetime.now(dt.tzinfo)
        seconds = (dt - now).total_seconds()
        return max(0.0, seconds)
    except Exception:
        return None


def should_record_landing_url(status_code: int, content_type: str) -> bool:
    if int(status_code) not in (401, 403):
        return False
    return "text/html" in (content_type or "").lower()
