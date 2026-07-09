import hashlib
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorLog,
    MonitorSpan,
    MonitorUsageEvent,
    MonitorUsageSource,
    MonitorWebVital,
)
from app.services.monitoring.analytics import _rate_vital
from app.services.monitoring.common import _now


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"\d+")

# Per-source ingest rate limit. Fixed one-minute window kept in process: a
# coarse flood guard so one source can't bury ingest, not a precise distributed
# quota (each worker holds its own window). A batch counts as its event total.
_rate_windows: dict[str, list] = {}
_SPAN_KINDS = ("client", "server", "internal")


def _normalize_error_message(message: str) -> str:
    text = (message or "").strip()
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _NUM_RE.sub("<n>", text)
    return " ".join(text.split())


def _top_stack_frame(stack: str | None) -> str:
    if not stack:
        return ""
    for raw in stack.splitlines():
        line = raw.strip()
        if line.startswith("at ") or line.startswith("File ") or "@" in line:
            return _NUM_RE.sub("<n>", line)
    return ""


def _error_fingerprint(message: str, stack: str | None) -> str:
    basis = f"{_normalize_error_message(message)}|{_top_stack_frame(stack)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


async def _error_group(pipeline_id: str, fingerprint: str, db: AsyncSession) -> MonitorErrorGroup | None:
    return (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.fingerprint == fingerprint,
        )
    )).scalar_one_or_none()


def _check_rate_limit(source_id: str, cost: int = 1) -> None:
    limit = get_settings().ingest_rate_limit_per_minute
    if limit <= 0:
        return
    window = int(_now().timestamp()) // 60
    entry = _rate_windows.get(source_id)
    if entry is None or entry[0] != window:
        # New window. Opportunistically drop stale entries so the map can't grow
        # unbounded across many sources.
        if len(_rate_windows) > 10000:
            _rate_windows.clear()
        entry = [window, 0]
        _rate_windows[source_id] = entry
    entry[1] += cost
    if entry[1] > limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "60"})


def _record_web_vital(source, *, metric, value, rating, url, navigation_id, visitor_id, session_id, user_ref, release, environment, occurred_at, now, trace_id=None, span_id=None, parent_span_id=None, platform=None):
    """Build a web-vital row (not committed). Rating falls back to the
    threshold-derived value when the client didn't send one. Shared by the
    single /vitals endpoint and /batch."""
    return MonitorWebVital(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        metric=metric,
        value=float(value),
        rating=rating or _rate_vital(metric, value),
        url=url,
        navigation_id=navigation_id,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        occurred_at=occurred_at or now,
        received_at=now,
    )


def _record_log(source, *, level, message, metadata, url, visitor_id, session_id, user_ref, release, environment, occurred_at, now, trace_id=None, span_id=None, parent_span_id=None, platform=None):
    """Build a log row (not committed). Shared by the single /logs endpoint and
    /batch."""
    return MonitorLog(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        level=level,
        message=str(message)[:4000],
        event_metadata=metadata or {},
        url=url,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        occurred_at=occurred_at or now,
        received_at=now,
    )


def _record_span(source, *, trace_id, span_id, parent_span_id, name, kind, service, feature, platform, capture_mode, status, release, environment, visitor_id, session_id, user_ref, attributes, start_at, duration_ms, now):
    """Build a span row (not committed). Shared by /spans and /batch. kind is
    clamped to the allowed set so a malformed client can't trip the CHECK."""
    return MonitorSpan(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=str(name)[:200],
        kind=kind if kind in _SPAN_KINDS else "client",
        service=service,
        feature=feature,
        platform=platform,
        capture_mode=capture_mode,
        status=status,
        release=release,
        environment=environment,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        attributes=attributes or {},
        start_at=start_at or now,
        duration_ms=duration_ms,
        received_at=now,
    )


def _build_usage_event(source, *, event_type, visitor_id, session_id, user_ref, url, referrer, release, environment, metadata, occurred_at, now, trace_id=None, span_id=None, parent_span_id=None, platform=None, capture_mode=None):
    """Build a usage event row (not added/committed). Shared by the single
    `/events` endpoint and `/batch` so both store identical shapes."""
    return MonitorUsageEvent(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        event_type=event_type,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        url=url,
        referrer=referrer,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        capture_mode=capture_mode,
        event_metadata=metadata or {},
        occurred_at=occurred_at or now,
        received_at=now,
    )


async def _record_error_event(source, db, *, message, stack, level, handled, url, release, environment, visitor_id, session_id, user_ref, metadata, occurred_at, now, error_type=None, trace_id=None, span_id=None, parent_span_id=None, platform=None, capture_mode=None):
    """Upsert the fingerprint group and add the error event (not committed).
    Shared by the single `/errors` endpoint and `/batch` so grouping, regression
    reopen, and first/last-seen tracking stay identical across both."""
    occurred = occurred_at or now
    fingerprint = _error_fingerprint(message, stack)
    title = (_normalize_error_message(message) or message)[:500]

    group = await _error_group(source.pipeline_id, fingerprint, db)
    if group is None:
        group = MonitorErrorGroup(
            id=str(uuid.uuid4()),
            pipeline_id=source.pipeline_id,
            source_id=source.id,
            fingerprint=fingerprint,
            title=title,
            level=level,
            error_type=error_type,
            status="unresolved",
            event_count=1,
            last_release=release,
            first_seen_at=occurred,
            last_seen_at=occurred,
            created_at=now,
            updated_at=now,
        )
        db.add(group)
    else:
        group.event_count = int(group.event_count or 0) + 1
        if group.last_seen_at is None or occurred > group.last_seen_at:
            group.last_seen_at = occurred
        if group.first_seen_at is None or occurred < group.first_seen_at:
            group.first_seen_at = occurred
        if release:
            group.last_release = release
        if error_type and not group.error_type:
            group.error_type = error_type
        # Regression: a previously resolved issue is happening again → reopen it.
        if group.status == "resolved":
            group.status = "unresolved"
        group.updated_at = now

    event = MonitorErrorEvent(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        group_id=group.id,
        fingerprint=fingerprint,
        message=message[:2000],
        stack=stack,
        level=level,
        handled=handled,
        error_type=error_type,
        url=url,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        capture_mode=capture_mode,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        event_metadata=metadata or {},
        occurred_at=occurred,
        received_at=now,
    )
    db.add(event)
    return event, group
