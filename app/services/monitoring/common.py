from datetime import date, datetime, timezone
from urllib.parse import urlparse

from fastapi import Request


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _domain_from_url(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    host = parsed.hostname or cleaned
    return host.lower().removeprefix("www.")


def _host_matches_allowed(host: str | None, allowed_domain: str | None) -> bool:
    host = _domain_from_url(host)
    allowed = _domain_from_url(allowed_domain)
    if not host or not allowed:
        return False
    return host == allowed or host.endswith(f".{allowed}")


def _origin_allowed(allowed_domain: str | None, request: Request, url: str | None) -> bool:
    if not allowed_domain:
        return True
    candidates = [
        request.headers.get("origin"),
        request.headers.get("referer"),
        url,
    ]
    return any(_host_matches_allowed(candidate, allowed_domain) for candidate in candidates)


def _pct_change(current: int, previous: int) -> float | None:
    """Week-over-week change as a fraction, or None when there's no baseline."""
    if not previous:
        return None
    return round((current - previous) / previous, 4)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
