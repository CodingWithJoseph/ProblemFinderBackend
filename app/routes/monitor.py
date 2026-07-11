import secrets
import uuid
from datetime import timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.config import get_settings
from app.db import get_db
from app.models import (
    MonitorAlertSettings,
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorInvestigation,
    MonitorInvestigationEntry,
    MonitorLog,
    MonitorProblem,
    MonitorReport,
    MonitorRevenueSource,
    MonitorSpan,
    MonitorUsageEvent,
    MonitorUsageSource,
    MonitorWebVital,
)
from app.services.alerts import AlertPrefs, alert_settings_row
from app.services.app_settings import effective_config
from app.services.monitoring.analytics import (
    _build_correlation_days,
    _compute_retention,
    _correlation_insights,
    _empty_daily,
    _empty_error_daily,
    _explorer_health,
    _feature_flow,
    _flow_graph,
    _health_verdict,
    _issue_trend,
    _rate_vital,
    _round_vital,
    _series_stats,
    _source_health,
    _VITAL_ORDER,
)
from app.services.monitoring.common import _clean_text, _domain_from_url, _iso, _now, _pct_change
from app.services.monitoring.serializers import (
    _order_span_tree,
    _serialize_alert_settings,
    _serialize_entry,
    _serialize_error_event,
    _serialize_error_group,
    _serialize_investigation,
    _serialize_issue,
    _serialize_problem,
    _serialize_report,
    _serialize_span,
    _serialize_usage_event,
    _serialize_usage_source,
)
from app.services.monitoring.sources import (
    _require_launched_product,
    _revenue_source,
    _revenue_source_is_connected,
    _selected_mrr,
    _usage_source,
    _usage_source_is_connected,
)

router = APIRouter(tags=["monitor"])
settings = get_settings()

UsageEventType = Literal["pageview", "signup", "login", "activation", "custom"]
ErrorLevel = Literal["error", "warning"]
WebVitalMetric = Literal["LCP", "CLS", "INP", "FCP", "TTFB"]
WebVitalRating = Literal["good", "needs-improvement", "poor"]
LogLevel = Literal["debug", "info", "warn", "error"]
















class UsageSourceUpdateBody(BaseModel):
    product_url: str | None = Field(default=None, max_length=2048)
    allowed_domain: str | None = Field(default=None, max_length=255)


class AlertSettingsBody(BaseModel):
    # All optional: only fields present in the request are applied. A null
    # threshold clears the override and reverts to the global default.
    new_issue_enabled: bool | None = None
    error_spike_enabled: bool | None = None
    signups_drop_enabled: bool | None = None
    revenue_drop_enabled: bool | None = None
    error_spike_multiplier: float | None = Field(default=None, ge=1.0, le=100.0)
    signups_drop_pct: float | None = Field(default=None, ge=0.05, le=1.0)
    revenue_drop_pct: float | None = Field(default=None, ge=0.05, le=1.0)


@router.get("/{pipeline_id}/health")
async def get_source_health(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """The v2 health verdict for a source, with the signals behind it."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    verdict, signals = await _source_health(pipeline_id, source, eff, now, db)
    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": eff.analytics_usage_window_days,
            "health": verdict,
            "signals": signals,
        }
    }


@router.get("/{pipeline_id}/command-center")
async def get_command_center(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """The at-a-glance Connect screen: the v2 health verdict, week-over-week
    trends (visitors / signups / errors / revenue), and the top open issues —
    one read that says 'is this product OK, and what needs attention'."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    growth_window = eff.analytics_growth_window_days
    week_start = now - timedelta(days=growth_window)
    prev_week_start = now - timedelta(days=2 * growth_window)

    verdict, signals = await _source_health(pipeline_id, source, eff, now, db)
    revenue = await _revenue_source(pipeline_id, db)

    async def _count(model, start, end, *extra):
        return int(await db.scalar(
            select(func.count(model.id)).where(
                model.pipeline_id == pipeline_id,
                model.occurred_at >= start,
                model.occurred_at < end,
                *extra,
            )
        ) or 0)

    async def _distinct_visitors(start, end):
        return int(await db.scalar(
            select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= start,
                MonitorUsageEvent.occurred_at < end,
                MonitorUsageEvent.visitor_id != None,
            )
        ) or 0)

    visitors_cur = await _distinct_visitors(week_start, now)
    visitors_prev = await _distinct_visitors(prev_week_start, week_start)
    signups_cur = await _count(MonitorUsageEvent, week_start, now, MonitorUsageEvent.event_type == "signup")
    signups_prev = await _count(MonitorUsageEvent, prev_week_start, week_start, MonitorUsageEvent.event_type == "signup")
    errors_cur = await _count(MonitorErrorEvent, week_start, now)
    errors_prev = await _count(MonitorErrorEvent, prev_week_start, week_start)

    def trend(current, previous):
        return {"current": int(current), "previous": int(previous), "changePct": _pct_change(current, previous)}

    rev_cur = revenue.current_mrr_cents if revenue else None
    rev_prev = revenue.previous_mrr_cents if revenue else None

    top_groups = list((await db.execute(
        select(MonitorErrorGroup)
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.status == "unresolved",
        )
        .order_by(MonitorErrorGroup.last_seen_at.desc())
        .limit(3)
    )).scalars().all())

    return {
        "data": {
            "windowDays": eff.analytics_usage_window_days,
            "growthWindowDays": growth_window,
            "health": verdict,
            "signals": signals,
            "trends": {
                "visitors": trend(visitors_cur, visitors_prev),
                "signups": trend(signups_cur, signups_prev),
                "errors": trend(errors_cur, errors_prev),
                "revenue": {
                    "current": rev_cur,
                    "previous": rev_prev,
                    "changePct": _pct_change(rev_cur, rev_prev) if (rev_cur is not None and rev_prev) else None,
                },
            },
            "revenueConnected": _revenue_source_is_connected(revenue, pipeline_id),
            "topIssues": [
                {
                    "id": group.id,
                    "title": group.title,
                    "level": group.level,
                    "status": group.status,
                    "eventCount": int(group.event_count or 0),
                    "lastRelease": group.last_release,
                    "lastSeenAt": _iso(group.last_seen_at),
                }
                for group in top_groups
            ],
        }
    }


@router.get("/{pipeline_id}/problems")
async def get_problems(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Monitoring problems for a product — the durable records the evaluator
    writes when a condition fires (3.4), most recent first."""
    await _require_launched_product(pipeline_id, db, uid)
    problems = list((await db.execute(
        select(MonitorProblem)
        .where(MonitorProblem.pipeline_id == pipeline_id)
        .order_by(MonitorProblem.detected_at.desc())
        .limit(50)
    )).scalars().all())
    return {"data": {"problems": [_serialize_problem(p) for p in problems]}}


@router.get("/{pipeline_id}/problems/{problem_id}")
async def get_problem_impact(
    pipeline_id: str,
    problem_id: str,
    window_hours: int = Query(default=24, ge=1, le=168, alias="windowHours"),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Problem impact (3.5): deterministic before / during / after counts of the
    problem's metric, in equal windows around when it was detected. Frames the
    blast radius — was it a blip or a sustained regression?"""
    await _require_launched_product(pipeline_id, db, uid)
    problem = (await db.execute(
        select(MonitorProblem).where(
            MonitorProblem.id == problem_id,
            MonitorProblem.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    detected = problem.detected_at
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=timezone.utc)
    span = timedelta(hours=window_hours)

    impact = {"metric": problem.metric, "windowHours": window_hours, "before": None, "during": None, "after": None}
    model_extra = {
        "errors": (MonitorErrorEvent, ()),
        "signups": (MonitorUsageEvent, (MonitorUsageEvent.event_type == "signup",)),
    }.get(problem.metric or "")
    if model_extra:
        model, extra = model_extra

        async def _count(start, end):
            return int(await db.scalar(
                select(func.count(model.id)).where(
                    model.pipeline_id == pipeline_id,
                    model.occurred_at >= start,
                    model.occurred_at < end,
                    *extra,
                )
            ) or 0)

        impact["before"] = await _count(detected - 2 * span, detected - span)
        impact["during"] = await _count(detected - span, detected)
        impact["after"] = await _count(detected, detected + span)

    return {"data": {"problem": _serialize_problem(problem), "impact": impact}}


# ── Phase 4: Investigate & Report ────────────────────────────────────────────

class InvestigationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)


class InvestigationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    status: Literal["open", "resolved", "archived"] | None = None


class EntryCreate(BaseModel):
    kind: Literal["note", "issue", "problem", "session", "link"]
    ref_id: str | None = Field(default=None, max_length=256)
    body: str | None = Field(default=None, max_length=8000)
    metadata: dict | None = None


class ReportCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=100000)
    investigation_id: str | None = None


async def _get_investigation(pipeline_id: str, investigation_id: str, db) -> MonitorInvestigation:
    inv = (await db.execute(
        select(MonitorInvestigation).where(
            MonitorInvestigation.id == investigation_id,
            MonitorInvestigation.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return inv


@router.post("/{pipeline_id}/investigations")
async def create_investigation(
    pipeline_id: str,
    body: InvestigationCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    now = _now()
    inv = MonitorInvestigation(
        id=str(uuid.uuid4()), pipeline_id=pipeline_id,
        title=body.title, summary=body.summary, status="open",
        created_at=now, updated_at=now,
    )
    db.add(inv)
    await db.commit()
    return {"data": _serialize_investigation(inv)}


@router.get("/{pipeline_id}/investigations")
async def list_investigations(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    rows = list((await db.execute(
        select(MonitorInvestigation)
        .where(MonitorInvestigation.pipeline_id == pipeline_id)
        .order_by(MonitorInvestigation.created_at.desc())
        .limit(100)
    )).scalars().all())
    return {"data": {"investigations": [_serialize_investigation(i) for i in rows]}}


@router.get("/{pipeline_id}/investigations/{investigation_id}")
async def get_investigation(
    pipeline_id: str,
    investigation_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """An investigation and its timeline — evidence + notes ordered by time."""
    await _require_launched_product(pipeline_id, db, uid)
    inv = await _get_investigation(pipeline_id, investigation_id, db)
    entries = list((await db.execute(
        select(MonitorInvestigationEntry)
        .where(MonitorInvestigationEntry.investigation_id == investigation_id)
        .order_by(MonitorInvestigationEntry.created_at.asc())
    )).scalars().all())
    return {
        "data": {
            "investigation": _serialize_investigation(inv),
            "timeline": [_serialize_entry(e) for e in entries],
        }
    }


@router.patch("/{pipeline_id}/investigations/{investigation_id}")
async def update_investigation(
    pipeline_id: str,
    investigation_id: str,
    body: InvestigationUpdate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    inv = await _get_investigation(pipeline_id, investigation_id, db)
    if body.title is not None:
        inv.title = body.title
    if body.summary is not None:
        inv.summary = body.summary
    if body.status is not None:
        inv.status = body.status
    inv.updated_at = _now()
    await db.commit()
    return {"data": _serialize_investigation(inv)}


@router.post("/{pipeline_id}/investigations/{investigation_id}/entries")
async def add_investigation_entry(
    pipeline_id: str,
    investigation_id: str,
    body: EntryCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Add a timeline entry: a note, or evidence linking an issue/problem/session."""
    await _require_launched_product(pipeline_id, db, uid)
    inv = await _get_investigation(pipeline_id, investigation_id, db)
    if body.kind == "note" and not (body.body or "").strip():
        raise HTTPException(status_code=422, detail="A note needs a body")
    if body.kind != "note" and not body.ref_id:
        raise HTTPException(status_code=422, detail="Evidence needs a ref_id")
    now = _now()
    entry = MonitorInvestigationEntry(
        id=str(uuid.uuid4()), investigation_id=investigation_id,
        kind=body.kind, ref_id=body.ref_id, body=body.body,
        event_metadata=body.metadata or {}, created_at=now,
    )
    db.add(entry)
    inv.updated_at = now
    await db.commit()
    return {"data": _serialize_entry(entry)}


@router.post("/{pipeline_id}/reports")
async def create_report(
    pipeline_id: str,
    body: ReportCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    now = _now()
    report = MonitorReport(
        id=str(uuid.uuid4()), pipeline_id=pipeline_id,
        investigation_id=body.investigation_id, title=body.title,
        body=body.body or "", created_at=now, updated_at=now,
    )
    db.add(report)
    await db.commit()
    return {"data": _serialize_report(report)}


@router.get("/{pipeline_id}/reports")
async def list_reports(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    rows = list((await db.execute(
        select(MonitorReport)
        .where(MonitorReport.pipeline_id == pipeline_id)
        .order_by(MonitorReport.created_at.desc())
        .limit(100)
    )).scalars().all())
    return {"data": {"reports": [_serialize_report(r) for r in rows]}}


async def _get_report(pipeline_id: str, report_id: str, db) -> MonitorReport:
    report = (await db.execute(
        select(MonitorReport).where(
            MonitorReport.id == report_id,
            MonitorReport.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/{pipeline_id}/reports/{report_id}")
async def get_report(
    pipeline_id: str,
    report_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    report = await _get_report(pipeline_id, report_id, db)
    return {"data": _serialize_report(report)}


@router.get("/{pipeline_id}/reports/{report_id}/export")
async def export_report(
    pipeline_id: str,
    report_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Exportable view: the report rendered as downloadable Markdown."""
    await _require_launched_product(pipeline_id, db, uid)
    report = await _get_report(pipeline_id, report_id, db)
    markdown = f"# {report.title}\n\n_Generated {_iso(report.updated_at)}_\n\n{report.body}\n"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="report-{report.id}.md"'},
    )


@router.post("/{pipeline_id}/usage-source")
async def create_usage_source(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _require_launched_product(pipeline_id, db, uid)
    existing = await _usage_source(product.id, db)
    if existing:
        return {"data": _serialize_usage_source(existing)}

    now = _now()
    source = MonitorUsageSource(
        id=str(uuid.uuid4()),
        pipeline_id=product.id,
        user_id=uid,
        public_key=secrets.token_urlsafe(24),
        status="connected",
        product_url=_clean_text(product.url),
        allowed_domain=_domain_from_url(product.url),
        created_at=now,
        updated_at=now,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_usage_source(source)}


@router.patch("/{pipeline_id}/usage-source")
async def update_usage_source(
    pipeline_id: str,
    body: UsageSourceUpdateBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _require_launched_product(pipeline_id, db, uid)
    source = await _usage_source(product.id, db)
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")

    source.product_url = _clean_text(body.product_url)
    source.allowed_domain = _domain_from_url(body.allowed_domain or body.product_url)
    source.updated_at = _now()
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_usage_source(source)}


@router.get("/{pipeline_id}/usage")
async def get_usage_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    growth_window = eff.analytics_growth_window_days
    since = now - timedelta(days=usage_window)
    week_start = now - timedelta(days=growth_window)
    prev_week_start = now - timedelta(days=2 * growth_window)

    total_events = await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(MonitorUsageEvent.pipeline_id == pipeline_id)
    ) or 0

    window_rows = (await db.execute(
        select(MonitorUsageEvent.event_type, func.count(MonitorUsageEvent.id))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
        .group_by(MonitorUsageEvent.event_type)
    )).all()
    counts = {event_type: int(count) for event_type, count in window_rows}

    visitors_14d = await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.visitor_id != None,
        )
    ) or 0
    active_users_14d = await db.scalar(
        select(func.count(func.distinct(func.coalesce(MonitorUsageEvent.user_ref, MonitorUsageEvent.visitor_id)))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
    ) or 0

    daily = _empty_daily(usage_window)
    daily_rows = (await db.execute(
        select(
            func.date(MonitorUsageEvent.occurred_at),
            MonitorUsageEvent.event_type,
            func.count(MonitorUsageEvent.id),
            func.count(func.distinct(MonitorUsageEvent.visitor_id)),
        )
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorUsageEvent.occurred_at), MonitorUsageEvent.event_type)
        .order_by(func.date(MonitorUsageEvent.occurred_at).asc())
    )).all()
    for day, event_type, count, visitors in daily_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key not in daily:
            continue
        daily[key]["events"] += int(count)
        if event_type == "pageview":
            daily[key]["pageviews"] += int(count)
            daily[key]["visitors"] = max(daily[key]["visitors"], int(visitors or 0))
        elif event_type == "signup":
            daily[key]["signups"] += int(count)
        elif event_type == "activation":
            daily[key]["activations"] += int(count)

    recent_events = list((await db.execute(
        select(MonitorUsageEvent)
        .where(MonitorUsageEvent.pipeline_id == pipeline_id)
        .order_by(MonitorUsageEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    # Aggregation engine v0 — the rollups the screen leads with; the raw
    # recent-events log above is secondary. Top pages: pageviews grouped by URL.
    top_pages = [
        {
            "url": url,
            "views": int(views),
            "visitors": int(visitors or 0),
            "lastSeenAt": _iso(last_seen),
        }
        for url, views, visitors, last_seen in (await db.execute(
            select(
                MonitorUsageEvent.url,
                func.count(MonitorUsageEvent.id),
                func.count(func.distinct(MonitorUsageEvent.visitor_id)),
                func.max(MonitorUsageEvent.occurred_at),
            )
            .where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= since,
                MonitorUsageEvent.event_type == "pageview",
                MonitorUsageEvent.url != None,
            )
            .group_by(MonitorUsageEvent.url)
            .order_by(func.count(MonitorUsageEvent.id).desc())
            .limit(10)
        )).all()
    ]

    # Top events by frequency ranks actions, not pageviews. A custom event's name
    # lives in metadata.name; typed events (signup/login/activation) fall back to
    # their event_type. Pageviews are excluded — they're the top-pages rollup.
    event_name = func.coalesce(MonitorUsageEvent.event_metadata["name"].astext, MonitorUsageEvent.event_type)
    top_events = [
        {
            "name": name,
            "count": int(count),
            "visitors": int(visitors or 0),
            "lastSeenAt": _iso(last_seen),
        }
        for name, count, visitors, last_seen in (await db.execute(
            select(
                event_name.label("name"),
                func.count(MonitorUsageEvent.id),
                func.count(func.distinct(MonitorUsageEvent.visitor_id)),
                func.max(MonitorUsageEvent.occurred_at),
            )
            .where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= since,
                MonitorUsageEvent.event_type != "pageview",
            )
            .group_by(event_name)
            .order_by(func.count(MonitorUsageEvent.id).desc())
            .limit(10)
        )).all()
    ]

    # Conversion funnel (14d): distinct visitors at each stage, so a few noisy
    # visitors firing many events don't distort the rates.
    stage_rows = (await db.execute(
        select(MonitorUsageEvent.event_type, func.count(func.distinct(MonitorUsageEvent.visitor_id)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.visitor_id != None,
            MonitorUsageEvent.event_type.in_(["signup", "activation"]),
        )
        .group_by(MonitorUsageEvent.event_type)
    )).all()
    stage_visitors = {event_type: int(count) for event_type, count in stage_rows}
    visited = int(visitors_14d)
    signed_up = stage_visitors.get("signup", 0)
    activated = stage_visitors.get("activation", 0)
    funnel = {
        "visited": visited,
        "signedUp": signed_up,
        "activated": activated,
        "signupRate": round(signed_up / visited, 4) if visited else None,
        "activationRate": round(activated / signed_up, 4) if signed_up else None,
    }

    # Week-over-week growth: this 7d window vs the prior 7d window.
    this_week_visitors = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= week_start,
            MonitorUsageEvent.visitor_id != None,
        )
    ) or 0)
    prev_week_visitors = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= prev_week_start,
            MonitorUsageEvent.occurred_at < week_start,
            MonitorUsageEvent.visitor_id != None,
        )
    ) or 0)
    this_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= week_start,
            MonitorUsageEvent.event_type == "signup",
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= prev_week_start,
            MonitorUsageEvent.occurred_at < week_start,
            MonitorUsageEvent.event_type == "signup",
        )
    ) or 0)
    growth = {
        "visitors": {
            "current": this_week_visitors,
            "previous": prev_week_visitors,
            "changePct": _pct_change(this_week_visitors, prev_week_visitors),
        },
        "signups": {
            "current": this_week_signups,
            "previous": prev_week_signups,
            "changePct": _pct_change(this_week_signups, prev_week_signups),
        },
    }

    # Retention: pull each visitor's first-seen day and their distinct active
    # days over a 30d window, then compute D1/D7 return in-process.
    retention_window_days = eff.analytics_retention_window_days
    retention_since = now - timedelta(days=retention_window_days)
    first_seen_rows = (await db.execute(
        select(MonitorUsageEvent.visitor_id, func.min(func.date(MonitorUsageEvent.occurred_at)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= retention_since,
            MonitorUsageEvent.visitor_id != None,
        )
        .group_by(MonitorUsageEvent.visitor_id)
    )).all()
    active_rows = (await db.execute(
        select(MonitorUsageEvent.visitor_id, func.date(MonitorUsageEvent.occurred_at))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= retention_since,
            MonitorUsageEvent.visitor_id != None,
        )
        .distinct()
    )).all()
    retention = _compute_retention(
        list(first_seen_rows), list(active_rows), now.date(), window_days=retention_window_days
    )

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "totalEvents": int(total_events),
            "lastSeenAt": _iso(source.last_seen_at if source else None),
            "health": _health_verdict(
                connected=_usage_source_is_connected(source, pipeline_id),
                last_seen_at=source.last_seen_at if source else None,
                total_events=int(total_events),
                eff=eff,
                now=now,
            ),
            "windowDays": usage_window,
            "growthWindowDays": growth_window,
            "summary14d": {
                "pageviews": counts.get("pageview", 0),
                "visitors": int(visitors_14d),
                "signups": counts.get("signup", 0),
                "logins": counts.get("login", 0),
                "activations": counts.get("activation", 0),
                "customEvents": counts.get("custom", 0),
                "activeUsers": int(active_users_14d),
            },
            "topPages": top_pages,
            "topEvents": top_events,
            "funnel": funnel,
            "growth": growth,
            "retention": retention,
            "daily": list(daily.values()),
            "recentEvents": [_serialize_usage_event(event) for event in recent_events],
        }
    }


@router.get("/{pipeline_id}/sessions")
async def get_sessions(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Sessions as first-class objects: each row is an aggregate of Monitor
    usage events sharing a session_id, not a raw event log. No sessions table
    until perf demands one."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    base = (
        MonitorUsageEvent.pipeline_id == pipeline_id,
        MonitorUsageEvent.occurred_at >= since,
        MonitorUsageEvent.session_id != None,
    )
    total_sessions = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(*base)
    ) or 0)
    identified_sessions = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            *base, MonitorUsageEvent.user_ref != None
        )
    ) or 0)
    total_events = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(*base)
    ) or 0)

    # The rollup: one aggregated row per session, most-recently-active first.
    pageviews = func.sum(case((MonitorUsageEvent.event_type == "pageview", 1), else_=0))
    rows = (await db.execute(
        select(
            MonitorUsageEvent.session_id,
            func.max(MonitorUsageEvent.visitor_id),
            func.max(MonitorUsageEvent.user_ref),
            func.min(MonitorUsageEvent.occurred_at),
            func.max(MonitorUsageEvent.occurred_at),
            func.count(MonitorUsageEvent.id),
            pageviews,
        )
        .where(*base)
        .group_by(MonitorUsageEvent.session_id)
        .order_by(func.max(MonitorUsageEvent.occurred_at).desc())
        .limit(50)
    )).all()

    sessions = []
    for session_id, visitor_id, user_ref, started, ended, events, page_count in rows:
        duration = int((ended - started).total_seconds()) if started and ended else 0
        sessions.append({
            "sessionId": session_id,
            "visitorId": visitor_id,
            "userRef": user_ref,
            "identified": user_ref is not None,
            "startedAt": _iso(started),
            "endedAt": _iso(ended),
            "durationSeconds": duration,
            "events": int(events),
            "pageviews": int(page_count or 0),
        })

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "summary": {
                "totalSessions": total_sessions,
                "identifiedSessions": identified_sessions,
                "avgEventsPerSession": round(total_events / total_sessions, 1) if total_sessions else None,
            },
            "sessions": sessions,
        }
    }


@router.get("/{pipeline_id}/sessions/{session_id}")
async def get_session_detail(
    pipeline_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """One session's timeline: its usage events and the errors that happened in
    it, merged in chronological order. Drill carries context — the error shows
    up in situ, next to what the visitor was doing when it fired."""
    await _require_launched_product(pipeline_id, db, uid)
    source = await _usage_source(pipeline_id, db)

    usage_events = list((await db.execute(
        select(MonitorUsageEvent)
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.session_id == session_id,
        )
        .order_by(MonitorUsageEvent.occurred_at.asc())
        .limit(500)
    )).scalars().all())
    error_events = list((await db.execute(
        select(MonitorErrorEvent)
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.session_id == session_id,
        )
        .order_by(MonitorErrorEvent.occurred_at.asc())
        .limit(500)
    )).scalars().all())

    if not usage_events and not error_events:
        raise HTTPException(status_code=404, detail="Session not found")

    items: list[tuple] = []
    for event in usage_events:
        meta = event.event_metadata or {}
        items.append((event.occurred_at, {
            "kind": "event",
            "id": event.id,
            "eventType": event.event_type,
            "name": meta.get("name"),
            "url": event.url,
            "metadata": meta,
            "occurredAt": _iso(event.occurred_at),
        }))
    for event in error_events:
        items.append((event.occurred_at, {
            "kind": "error",
            "id": event.id,
            "groupId": event.group_id,
            "fingerprint": event.fingerprint,
            "message": event.message,
            "level": event.level,
            "url": event.url,
            "occurredAt": _iso(event.occurred_at),
        }))
    items.sort(key=lambda item: item[0])
    timeline = [payload for _, payload in items]

    both = usage_events + error_events
    started = items[0][0]
    ended = items[-1][0]
    user_ref = next((e.user_ref for e in both if e.user_ref), None)
    visitor_id = next((e.visitor_id for e in both if e.visitor_id), None)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "session": {
                "sessionId": session_id,
                "visitorId": visitor_id,
                "userRef": user_ref,
                "identified": user_ref is not None,
                "startedAt": _iso(started),
                "endedAt": _iso(ended),
                "durationSeconds": int((ended - started).total_seconds()) if started and ended else 0,
                "events": len(usage_events),
                "pageviews": sum(1 for e in usage_events if e.event_type == "pageview"),
                "errors": len(error_events),
            },
            "timeline": timeline,
        }
    }


@router.get("/{pipeline_id}/timeseries")
async def get_timeseries(
    pipeline_id: str,
    metric: Literal["errors", "loads"] = "errors",
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Over-time hero chart for one metric: daily points over the window, a
    normal-range baseline band (mean +/- stddev), and deploy markers (the first
    day each release appeared) so a spike can be read against 'what's normal' and
    lined up with the deploy that caused it."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)
    keys = sorted(_empty_daily(usage_window).keys())

    if metric == "errors":
        series_rows = (await db.execute(
            select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
            .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.occurred_at >= since)
            .group_by(func.date(MonitorErrorEvent.occurred_at))
        )).all()
    else:
        series_rows = (await db.execute(
            select(func.date(MonitorUsageEvent.occurred_at), func.count(MonitorUsageEvent.id))
            .where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= since,
                MonitorUsageEvent.event_type == "pageview",
            )
            .group_by(func.date(MonitorUsageEvent.occurred_at))
        )).all()
    counts: dict[str, int] = {}
    for day, count in series_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        counts[key] = int(count)
    points = [{"date": key, "value": counts.get(key, 0)} for key in keys]
    baseline = _series_stats([p["value"] for p in points])

    # Deploy markers: the first day each release was seen (a release rollout).
    marker_rows = (await db.execute(
        select(MonitorUsageEvent.release, func.min(func.date(MonitorUsageEvent.occurred_at)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.release != None,
        )
        .group_by(MonitorUsageEvent.release)
    )).all()
    markers = []
    for release, first_day in marker_rows:
        key = first_day.isoformat() if hasattr(first_day, "isoformat") else str(first_day)
        markers.append({"date": key, "release": release})
    markers.sort(key=lambda m: m["date"])

    return {
        "data": {
            "metric": metric,
            "windowDays": usage_window,
            "points": points,
            "baseline": baseline,
            "markers": markers,
        }
    }


@router.get("/{pipeline_id}/explorer")
async def get_explorer(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Multi-metric explorer: one row per page with loads, error rate, felt-speed
    (LCP p75 + rating), a health badge, and a per-row loads sparkline — the
    sortable 'where do I look first' table."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)
    window_dates = sorted(_empty_daily(usage_window).keys())

    load_rows = (await db.execute(
        select(MonitorUsageEvent.url, func.count(MonitorUsageEvent.id))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.event_type == "pageview",
            MonitorUsageEvent.url != None,
        )
        .group_by(MonitorUsageEvent.url)
        .order_by(func.count(MonitorUsageEvent.id).desc())
        .limit(25)
    )).all()
    urls = [url for url, _ in load_rows]

    error_rows = (await db.execute(
        select(MonitorErrorEvent.url, func.count(MonitorErrorEvent.id))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.url.in_(urls) if urls else False,
        )
        .group_by(MonitorErrorEvent.url)
    )).all()
    errors_by_url = {url: int(count) for url, count in error_rows}

    lcp_rows = (await db.execute(
        select(
            MonitorWebVital.url,
            func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc()),
        )
        .where(
            MonitorWebVital.pipeline_id == pipeline_id,
            MonitorWebVital.occurred_at >= since,
            MonitorWebVital.metric == "LCP",
            MonitorWebVital.url.in_(urls) if urls else False,
        )
        .group_by(MonitorWebVital.url)
    )).all()
    lcp_by_url = {url: p75 for url, p75 in lcp_rows}

    spark_rows = (await db.execute(
        select(MonitorUsageEvent.url, func.date(MonitorUsageEvent.occurred_at), func.count(MonitorUsageEvent.id))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.event_type == "pageview",
            MonitorUsageEvent.url.in_(urls) if urls else False,
        )
        .group_by(MonitorUsageEvent.url, func.date(MonitorUsageEvent.occurred_at))
    )).all()
    spark_by_url: dict[str, dict[str, int]] = {}
    for url, day, count in spark_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        spark_by_url.setdefault(url, {})[key] = int(count)

    rows = []
    for url, loads in load_rows:
        loads = int(loads)
        errors = errors_by_url.get(url, 0)
        error_rate = round(errors / loads, 4) if loads else None
        lcp_p75 = _round_vital("LCP", lcp_by_url.get(url))
        lcp_rating = _rate_vital("LCP", lcp_p75)
        daily = spark_by_url.get(url, {})
        rows.append({
            "url": url,
            "loads": loads,
            "errors": errors,
            "errorRate": error_rate,
            "lcpP75": lcp_p75,
            "lcpRating": lcp_rating,
            "health": _explorer_health(error_rate, lcp_rating, loads),
            "spark": [daily.get(d, 0) for d in window_dates],
        })

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "rows": rows,
        }
    }


@router.get("/{pipeline_id}/traces/{trace_id}")
async def get_trace(
    pipeline_id: str,
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """One trace, assembled into the incident chain: the span tree (client root →
    fetch → server) with the errors and logs that share the trace_id pinned to
    it, so a frontend error can be followed to the backend work that caused it."""
    await _require_launched_product(pipeline_id, db, uid)

    spans = list((await db.execute(
        select(MonitorSpan)
        .where(MonitorSpan.pipeline_id == pipeline_id, MonitorSpan.trace_id == trace_id)
        .order_by(MonitorSpan.start_at.asc())
    )).scalars().all())
    errors = list((await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.trace_id == trace_id)
        .order_by(MonitorErrorEvent.occurred_at.asc())
    )).scalars().all())
    logs = list((await db.execute(
        select(MonitorLog)
        .where(MonitorLog.pipeline_id == pipeline_id, MonitorLog.trace_id == trace_id)
        .order_by(MonitorLog.occurred_at.asc())
    )).scalars().all())

    span_payloads = [_serialize_span(span, depth) for span, depth in _order_span_tree(spans)]
    services: list[str] = []
    for span in spans:
        if span.service and span.service not in services:
            services.append(span.service)
    durations = [s.duration_ms for s in spans if s.duration_ms is not None]

    return {
        "data": {
            "traceId": trace_id,
            "spans": span_payloads,
            "errors": [_serialize_error_event(e) for e in errors],
            "logs": [{
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "url": log.url,
                "spanId": log.span_id,
                "occurredAt": _iso(log.occurred_at),
            } for log in logs],
            "summary": {
                "spanCount": len(spans),
                "errorCount": len(errors),
                "logCount": len(logs),
                "services": services,
                "hasServer": any(s.kind == "server" for s in spans),
                "durationMs": max(durations) if durations else None,
            },
        }
    }


@router.get("/{pipeline_id}/flow")
async def get_flow(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Journey / flow graph: how one page leads into the next, aggregated from
    the ordered pageviews within each session — the top entry pages and the most
    common page→page paths."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    rows = (await db.execute(
        select(MonitorUsageEvent.session_id, MonitorUsageEvent.url, MonitorUsageEvent.occurred_at)
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.event_type == "pageview",
            MonitorUsageEvent.session_id != None,
            MonitorUsageEvent.url != None,
        )
        .order_by(MonitorUsageEvent.session_id.asc(), MonitorUsageEvent.occurred_at.asc())
        .limit(5000)
    )).all()
    nodes, edges = _flow_graph(rows)

    return {
        "data": {
            "windowDays": usage_window,
            "nodes": nodes,
            "edges": edges,
        }
    }


@router.get("/{pipeline_id}/feature-flow")
async def get_feature_flow(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Feature-flow graph: the named user flows (from instrumented feature spans)
    and how one leads into the next, with run count, error count and mean
    duration per feature. Empty until flows are instrumented — the UI falls back
    to the URL flow in that case."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    rows = (await db.execute(
        select(MonitorSpan.session_id, MonitorSpan.feature, MonitorSpan.status, MonitorSpan.duration_ms, MonitorSpan.start_at)
        .where(
            MonitorSpan.pipeline_id == pipeline_id,
            MonitorSpan.start_at >= since,
            MonitorSpan.feature != None,
            MonitorSpan.session_id != None,
        )
        .order_by(MonitorSpan.session_id.asc(), MonitorSpan.start_at.asc())
        .limit(5000)
    )).all()
    nodes, edges = _feature_flow(rows)

    return {
        "data": {
            "windowDays": usage_window,
            "nodes": nodes,
            "edges": edges,
        }
    }


@router.get("/{pipeline_id}/experience")
async def get_experience_vitals(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Experience vitals rollup: per-metric p75 + rating distribution, and the
    same per URL. Leads with the verdict (p75 → good/needs-improvement/poor), not
    the raw samples."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    metric_rows = (await db.execute(
        select(
            MonitorWebVital.metric,
            func.count(MonitorWebVital.id),
            func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc()),
        )
        .where(MonitorWebVital.pipeline_id == pipeline_id, MonitorWebVital.occurred_at >= since)
        .group_by(MonitorWebVital.metric)
    )).all()

    rating_rows = (await db.execute(
        select(MonitorWebVital.metric, MonitorWebVital.rating, func.count(MonitorWebVital.id))
        .where(MonitorWebVital.pipeline_id == pipeline_id, MonitorWebVital.occurred_at >= since)
        .group_by(MonitorWebVital.metric, MonitorWebVital.rating)
    )).all()

    url_rows = (await db.execute(
        select(
            MonitorWebVital.url,
            MonitorWebVital.metric,
            func.count(MonitorWebVital.id),
            func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc()),
        )
        .where(
            MonitorWebVital.pipeline_id == pipeline_id,
            MonitorWebVital.occurred_at >= since,
            MonitorWebVital.url != None,
        )
        .group_by(MonitorWebVital.url, MonitorWebVital.metric)
        .order_by(func.count(MonitorWebVital.id).desc())
        .limit(60)
    )).all()

    ratings: dict[str, dict] = {}
    for metric, rating, count in rating_rows:
        bucket = ratings.setdefault(metric, {"good": 0, "needs-improvement": 0, "poor": 0})
        if rating in bucket:
            bucket[rating] = int(count)

    metrics = []
    for metric, count, p75 in metric_rows:
        value = _round_vital(metric, p75)
        bucket = ratings.get(metric, {})
        metrics.append({
            "metric": metric,
            "sampleCount": int(count),
            "p75": value,
            "rating": _rate_vital(metric, value),
            "good": bucket.get("good", 0),
            "needsImprovement": bucket.get("needs-improvement", 0),
            "poor": bucket.get("poor", 0),
        })
    metrics.sort(key=lambda m: _VITAL_ORDER.get(m["metric"], 99))

    pages: dict[str, dict] = {}
    for url, metric, count, p75 in url_rows:
        page = pages.setdefault(url, {"url": url, "sampleCount": 0, "metrics": {}})
        page["sampleCount"] += int(count)
        value = _round_vital(metric, p75)
        page["metrics"][metric] = {"p75": value, "rating": _rate_vital(metric, value)}
    pages_list = sorted(pages.values(), key=lambda p: p["sampleCount"], reverse=True)[:10]

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "totalSamples": sum(m["sampleCount"] for m in metrics),
            "metrics": metrics,
            "pages": pages_list,
        }
    }


@router.get("/{pipeline_id}/logs")
async def get_logs(
    pipeline_id: str,
    level: LogLevel | None = None,
    q: str | None = Query(default=None, max_length=200),
    session: str | None = None,
    release: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Faceted log explorer: leads with the per-level counts (the rollup), then
    the filtered lines. Facets — level, message search (q), session, release —
    compose; the level counts reflect the other filters so the facet is live."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    # Filters other than level apply to both the level facet and the lines, so
    # the facet shows the level distribution within the current context.
    base = [MonitorLog.pipeline_id == pipeline_id, MonitorLog.occurred_at >= since]
    if q:
        base.append(MonitorLog.message.ilike(f"%{q}%"))
    if session:
        base.append(MonitorLog.session_id == session)
    if release:
        base.append(MonitorLog.release == release)

    level_rows = (await db.execute(
        select(MonitorLog.level, func.count(MonitorLog.id)).where(*base).group_by(MonitorLog.level)
    )).all()
    level_counts = {lvl: 0 for lvl in ("debug", "info", "warn", "error")}
    for lvl, count in level_rows:
        if lvl in level_counts:
            level_counts[lvl] = int(count)

    line_filters = list(base)
    if level:
        line_filters.append(MonitorLog.level == level)
    logs = list((await db.execute(
        select(MonitorLog)
        .where(*line_filters)
        .order_by(MonitorLog.occurred_at.desc())
        .limit(limit)
    )).scalars().all())

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "levelCounts": level_counts,
            "filters": {"level": level, "q": q, "session": session, "release": release},
            "logs": [
                {
                    "id": log.id,
                    "level": log.level,
                    "message": log.message,
                    "url": log.url,
                    "sessionId": log.session_id,
                    "userRef": log.user_ref,
                    "release": log.release,
                    "metadata": log.event_metadata or {},
                    "occurredAt": _iso(log.occurred_at),
                }
                for log in logs
            ],
        }
    }


# NOTE: this is the usage-vs-errors correlation. It is slated for a later rename
# to `/insights/error-correlation` to disambiguate from the new usage↔revenue
# correlation (`/insights/revenue-correlation`). Left at its current path here to
# keep this build non-breaking.
@router.get("/{pipeline_id}/correlation")
@router.get("/{pipeline_id}/insights/error-correlation")
async def get_correlation(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    now = _now()
    correlation_window = eff.analytics_correlation_window_days
    growth_window = eff.analytics_growth_window_days
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=correlation_window)
    week_start = now - timedelta(days=growth_window)
    prev_week_start = now - timedelta(days=2 * growth_window)
    usage_window_ago = now - timedelta(days=usage_window)

    usage_rows = (await db.execute(
        select(
            func.date(MonitorUsageEvent.occurred_at),
            MonitorUsageEvent.event_type,
            func.count(MonitorUsageEvent.id),
            func.count(func.distinct(MonitorUsageEvent.visitor_id)),
        )
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorUsageEvent.occurred_at), MonitorUsageEvent.event_type)
    )).all()
    error_rows = (await db.execute(
        select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorErrorEvent.occurred_at))
    )).all()

    days = _build_correlation_days(list(usage_rows), list(error_rows), days=correlation_window, today=now.date())
    insights = _correlation_insights(days)

    revenue = await db.scalar(
        select(MonitorRevenueSource).where(
            MonitorRevenueSource.pipeline_id == pipeline_id,
            MonitorRevenueSource.provider == "stripe",
        )
    )
    this_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
            MonitorUsageEvent.occurred_at >= week_start,
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
            MonitorUsageEvent.occurred_at >= prev_week_start,
            MonitorUsageEvent.occurred_at < week_start,
        )
    ) or 0)
    errors_14d = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= usage_window_ago,
        )
    ) or 0)
    sessions_14d = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= usage_window_ago,
            MonitorUsageEvent.session_id != None,
        )
    ) or 0)

    mrr, prev_mrr = _selected_mrr(revenue, eff.revenue_engine)

    return {
        "data": {
            "days": days,
            "insights": insights,
            "windowDays": correlation_window,
            "summary": {
                "mrrCents": mrr,
                "mrrChangePct": _pct_change(mrr, prev_mrr) if (mrr is not None and prev_mrr) else None,
                "signupsChangePct": _pct_change(this_week_signups, prev_week_signups),
                "errorsPerSession14d": round(errors_14d / sessions_14d, 4) if sessions_14d else None,
                "errorsPerSessionWindowDays": usage_window,
            },
        }
    }


@router.get("/{pipeline_id}/alert-settings")
async def get_alert_settings(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    row = await alert_settings_row(db, pipeline_id)
    return {"data": _serialize_alert_settings(AlertPrefs.resolve(row))}


@router.put("/{pipeline_id}/alert-settings")
async def update_alert_settings(
    pipeline_id: str,
    body: AlertSettingsBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    row = await alert_settings_row(db, pipeline_id)
    if row is None:
        row = MonitorAlertSettings(pipeline_id=pipeline_id)
        db.add(row)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    return {"data": _serialize_alert_settings(AlertPrefs.resolve(row))}


def _error_dimension_filters(pipeline_id: str, since, *, error_type: str | None, platform: str | None) -> list:
    """Group-level WHERE conditions for the error_type / platform dimension
    filters, shared by the errors + issues lists. error_type is stored on the
    group; platform lives on events, so a platform filter keeps groups that have
    at least one occurrence on that platform in the window."""
    conditions = []
    if error_type:
        conditions.append(MonitorErrorGroup.error_type == error_type)
    if platform:
        conditions.append(MonitorErrorGroup.id.in_(
            select(MonitorErrorEvent.group_id).where(
                MonitorErrorEvent.pipeline_id == pipeline_id,
                MonitorErrorEvent.platform == platform,
                MonitorErrorEvent.occurred_at >= since,
            ).distinct()
        ))
    return conditions


async def _error_dimension_facets(pipeline_id: str, since, db: AsyncSession) -> dict:
    """Available error_type / platform values (with counts) over the window, for
    the filter chips. Computed independent of the active filter so a chip never
    disappears just because it's selected. error_type is group-grained; platform
    is event-grained, counted as the distinct groups seen on each platform."""
    error_type_rows = (await db.execute(
        select(MonitorErrorGroup.error_type, func.count(MonitorErrorGroup.id))
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.last_seen_at >= since,
            MonitorErrorGroup.error_type.isnot(None),
        )
        .group_by(MonitorErrorGroup.error_type)
        .order_by(func.count(MonitorErrorGroup.id).desc())
    )).all()
    platform_rows = (await db.execute(
        select(MonitorErrorEvent.platform, func.count(func.distinct(MonitorErrorEvent.group_id)))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.platform.isnot(None),
        )
        .group_by(MonitorErrorEvent.platform)
        .order_by(func.count(func.distinct(MonitorErrorEvent.group_id)).desc())
    )).all()
    return {
        "errorType": [{"value": v, "count": int(c)} for v, c in error_type_rows],
        "platform": [{"value": v, "count": int(c)} for v, c in platform_rows],
    }


@router.get("/{pipeline_id}/errors")
async def get_error_metrics(
    pipeline_id: str,
    error_type: str | None = Query(default=None, alias="errorType", max_length=40),
    platform: str | None = Query(default=None, max_length=20),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    # The dimension filters ("choose what you want to see") scope the issue list
    # and recent-error feed; the health headline + trend stay global so the
    # filter narrows detail without hiding the product's overall error picture.
    event_filters = []
    if error_type:
        event_filters.append(MonitorErrorEvent.error_type == error_type)
    if platform:
        event_filters.append(MonitorErrorEvent.platform == platform)

    total_errors = await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(MonitorErrorEvent.pipeline_id == pipeline_id)
    ) or 0
    errors_14d = await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0
    affected_sessions_14d = await db.scalar(
        select(func.count(func.distinct(MonitorErrorEvent.session_id))).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.session_id != None,
        )
    ) or 0
    # Denominator for a rate (not a raw count): a traffic spike shouldn't read
    # as an error spike. Uses the usage sessions already collected for the product.
    usage_sessions_14d = await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.session_id != None,
        )
    ) or 0
    open_issues = await db.scalar(
        select(func.count(MonitorErrorGroup.id)).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.status == "unresolved",
        )
    ) or 0

    daily = _empty_error_daily(usage_window)
    daily_rows = (await db.execute(
        select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorErrorEvent.occurred_at))
        .order_by(func.date(MonitorErrorEvent.occurred_at).asc())
    )).all()
    for day, count in daily_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key in daily:
            daily[key]["errors"] += int(count)

    top_groups = list((await db.execute(
        select(MonitorErrorGroup)
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.last_seen_at >= since,
            *_error_dimension_filters(pipeline_id, since, error_type=error_type, platform=platform),
        )
        .order_by(MonitorErrorGroup.event_count.desc(), MonitorErrorGroup.last_seen_at.desc())
        .limit(10)
    )).scalars().all())

    session_rows = (await db.execute(
        select(MonitorErrorEvent.group_id, func.count(func.distinct(MonitorErrorEvent.session_id)))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.session_id != None,
        )
        .group_by(MonitorErrorEvent.group_id)
    )).all()
    sessions_by_group = {group_id: int(count) for group_id, count in session_rows}

    recent_errors = list((await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, *event_filters)
        .order_by(MonitorErrorEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    errors_per_session = round(int(errors_14d) / int(usage_sessions_14d), 4) if usage_sessions_14d else None

    facets = await _error_dimension_facets(pipeline_id, since, db)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "totalErrors": int(total_errors),
            "lastSeenAt": _iso(source.last_seen_at if source else None),
            "windowDays": usage_window,
            "summary14d": {
                "errors": int(errors_14d),
                "openIssues": int(open_issues),
                "affectedSessions": int(affected_sessions_14d),
                "errorsPerSession": errors_per_session,
            },
            "daily": list(daily.values()),
            "issues": [
                _serialize_error_group(group, affected_sessions=sessions_by_group.get(group.id, 0))
                for group in top_groups
            ],
            "recentErrors": [_serialize_error_event(event) for event in recent_errors],
            "facets": facets,
            "filters": {"errorType": error_type, "platform": platform},
        }
    }


@router.get("/{pipeline_id}/issues")
async def get_issues(
    pipeline_id: str,
    error_type: str | None = Query(default=None, alias="errorType", max_length=40),
    platform: str | None = Query(default=None, max_length=20),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Issue promotion engine: fingerprint groups → issue objects ranked by
    affected users. Affected-users/sessions and the trend are computed here from
    the raw error events (the group row only stores occurrences + first/last
    seen)."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=usage_window)
    midpoint = now - timedelta(days=usage_window / 2)
    actor = func.coalesce(MonitorErrorEvent.user_ref, MonitorErrorEvent.visitor_id)

    open_issues = int(await db.scalar(
        select(func.count(MonitorErrorGroup.id)).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.status == "unresolved",
            MonitorErrorGroup.last_seen_at >= since,
        )
    ) or 0)
    affected_users_total = int(await db.scalar(
        select(func.count(func.distinct(actor))).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0)
    occurrences_total = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0)

    # One pass over the window's events → per-group impact + a split into this
    # half / prior half for the trend.
    agg_rows = (await db.execute(
        select(
            MonitorErrorEvent.group_id,
            func.count(func.distinct(actor)),
            func.count(func.distinct(MonitorErrorEvent.session_id)),
            func.count(MonitorErrorEvent.id),
            func.sum(case((MonitorErrorEvent.occurred_at >= midpoint, 1), else_=0)),
            func.sum(case((MonitorErrorEvent.occurred_at < midpoint, 1), else_=0)),
        )
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.group_id != None,
        )
        .group_by(MonitorErrorEvent.group_id)
    )).all()
    agg = {
        group_id: (int(users), int(sessions), int(occ), int(recent or 0), int(prior or 0))
        for group_id, users, sessions, occ, recent, prior in agg_rows
    }

    groups = list((await db.execute(
        select(MonitorErrorGroup)
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.last_seen_at >= since,
            *_error_dimension_filters(pipeline_id, since, error_type=error_type, platform=platform),
        )
        .limit(200)
    )).scalars().all())

    issues = []
    for group in groups:
        users, sessions, occ, recent, prior = agg.get(group.id, (0, 0, 0, 0, 0))
        issues.append(_serialize_issue(
            group,
            affected_users=users,
            affected_sessions=sessions,
            occurrences=occ,
            trend=_issue_trend(recent, prior),
        ))
    # Ranked by affected users — the impact ordering the issues list leads with.
    issues.sort(key=lambda issue: (issue["affectedUsers"], issue["occurrences"]), reverse=True)
    issues = issues[:50]

    facets = await _error_dimension_facets(pipeline_id, since, db)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "summary": {
                "openIssues": open_issues,
                "affectedUsers": affected_users_total,
                "occurrences": occurrences_total,
            },
            "issues": issues,
            "facets": facets,
            "filters": {"errorType": error_type, "platform": platform},
        }
    }


@router.get("/{pipeline_id}/issues/{group_id}")
async def get_issue_detail(
    pipeline_id: str,
    group_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """One issue, interrogable: the promoted issue object plus faceted breakdowns
    (by release, by URL) so you can pivot on 'which deploy / which page', a sample
    stack, and recent occurrences."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=usage_window)
    midpoint = now - timedelta(days=usage_window / 2)
    actor = func.coalesce(MonitorErrorEvent.user_ref, MonitorErrorEvent.visitor_id)

    group = (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.id == group_id,
            MonitorErrorGroup.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Issue not found")

    scoped = (
        MonitorErrorEvent.pipeline_id == pipeline_id,
        MonitorErrorEvent.group_id == group_id,
        MonitorErrorEvent.occurred_at >= since,
    )
    agg = (await db.execute(
        select(
            func.count(func.distinct(actor)),
            func.count(func.distinct(MonitorErrorEvent.session_id)),
            func.count(MonitorErrorEvent.id),
            func.sum(case((MonitorErrorEvent.occurred_at >= midpoint, 1), else_=0)),
            func.sum(case((MonitorErrorEvent.occurred_at < midpoint, 1), else_=0)),
        ).where(*scoped)
    )).all()
    users, sessions, occ, recent, prior = (
        (int(agg[0][0] or 0), int(agg[0][1] or 0), int(agg[0][2] or 0), int(agg[0][3] or 0), int(agg[0][4] or 0))
        if agg else (0, 0, 0, 0, 0)
    )

    # Facets: slice this issue's occurrences by a dimension. Pivoting is the verb.
    release_label = func.coalesce(MonitorErrorEvent.release, "(none)")
    release_rows = (await db.execute(
        select(release_label, func.count(MonitorErrorEvent.id), func.count(func.distinct(actor)))
        .where(*scoped)
        .group_by(release_label)
        .order_by(func.count(MonitorErrorEvent.id).desc())
        .limit(20)
    )).all()
    url_label = func.coalesce(MonitorErrorEvent.url, "(none)")
    url_rows = (await db.execute(
        select(url_label, func.count(MonitorErrorEvent.id), func.count(func.distinct(actor)))
        .where(*scoped)
        .group_by(url_label)
        .order_by(func.count(MonitorErrorEvent.id).desc())
        .limit(20)
    )).all()

    sample = (await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.group_id == group_id)
        .order_by(MonitorErrorEvent.occurred_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    recent_events = list((await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.group_id == group_id)
        .order_by(MonitorErrorEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    sample_payload = None
    if sample:
        sample_payload = {
            "message": sample.message,
            "stack": sample.stack,
            "level": sample.level,
            "handled": sample.handled,
            "url": sample.url,
            "release": sample.release,
            "occurredAt": _iso(sample.occurred_at),
        }

    return {
        "data": {
            "issue": _serialize_issue(
                group,
                affected_users=users,
                affected_sessions=sessions,
                occurrences=occ,
                trend=_issue_trend(recent, prior),
            ),
            "sample": sample_payload,
            "facets": {
                "release": [{"value": value, "count": int(count), "users": int(u)} for value, count, u in release_rows],
                "url": [{"value": value, "count": int(count), "users": int(u)} for value, count, u in url_rows],
            },
            "recentOccurrences": [_serialize_error_event(event) for event in recent_events],
            "windowDays": usage_window,
        }
    }


class IssueStatusBody(BaseModel):
    status: Literal["unresolved", "resolved", "ignored"]


@router.get("/{pipeline_id}/issues/{group_id}/sessions")
async def get_issue_sessions(
    pipeline_id: str,
    group_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """The sessions this issue fired in — the issue → affected sessions → session
    timeline thread (where the error shows up in situ). Aggregated from the
    group's error events by session_id."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    group = (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.id == group_id,
            MonitorErrorGroup.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Issue not found")

    rows = (await db.execute(
        select(
            MonitorErrorEvent.session_id,
            func.max(MonitorErrorEvent.visitor_id),
            func.max(MonitorErrorEvent.user_ref),
            func.count(MonitorErrorEvent.id),
            func.min(MonitorErrorEvent.occurred_at),
            func.max(MonitorErrorEvent.occurred_at),
        )
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.group_id == group_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.session_id != None,
        )
        .group_by(MonitorErrorEvent.session_id)
        .order_by(func.max(MonitorErrorEvent.occurred_at).desc())
        .limit(50)
    )).all()

    sessions = []
    for session_id, visitor_id, user_ref, occurrences, first_seen, last_seen in rows:
        sessions.append({
            "sessionId": session_id,
            "visitorId": visitor_id,
            "userRef": user_ref,
            "identified": user_ref is not None,
            "occurrences": int(occurrences),
            "firstSeenAt": _iso(first_seen),
            "lastSeenAt": _iso(last_seen),
        })

    return {
        "data": {
            "issueId": group_id,
            "windowDays": usage_window,
            "sessions": sessions,
        }
    }


@router.patch("/{pipeline_id}/issues/{group_id}")
async def update_issue_status(
    pipeline_id: str,
    group_id: str,
    body: IssueStatusBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Manually set an issue's status. Resolve to clear it from open issues;
    ingest auto-reopens a resolved issue if it recurs (regression). Ignore mutes
    it — recurrence does not reopen an ignored issue."""
    await _require_launched_product(pipeline_id, db, uid)
    group = (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.id == group_id,
            MonitorErrorGroup.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Issue not found")
    group.status = body.status
    group.updated_at = _now()
    await db.commit()
    return {"data": {"id": group.id, "status": group.status}}


@router.get("/{pipeline_id}/errors/by-release")
async def get_errors_by_release(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Split-by-release across all errors: did this deploy regress? Per release we
    compute the error rate (errors / sessions) so a traffic difference between
    releases doesn't read as a regression on raw counts. Errors carry `release`
    natively; usage sessions carry it since B.4."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=usage_window)
    actor = func.coalesce(MonitorErrorEvent.user_ref, MonitorErrorEvent.visitor_id)
    error_release = func.coalesce(MonitorErrorEvent.release, "(none)")
    usage_release = func.coalesce(MonitorUsageEvent.release, "(none)")

    error_rows = (await db.execute(
        select(
            error_release,
            func.count(MonitorErrorEvent.id),
            func.count(func.distinct(actor)),
            func.count(func.distinct(MonitorErrorEvent.session_id)),
            func.min(MonitorErrorEvent.occurred_at),
            func.max(MonitorErrorEvent.occurred_at),
        )
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.occurred_at >= since)
        .group_by(error_release)
    )).all()

    # Sessions per release from usage, the denominator for the rate.
    session_rows = (await db.execute(
        select(usage_release, func.count(func.distinct(MonitorUsageEvent.session_id)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.session_id != None,
        )
        .group_by(usage_release)
    )).all()
    sessions_by_release = {release: int(count) for release, count in session_rows}

    releases = []
    for release, errors, users, err_sessions, first_seen, last_seen in error_rows:
        sessions = sessions_by_release.get(release, 0)
        releases.append({
            "release": release,
            "errors": int(errors),
            "affectedUsers": int(users),
            "affectedSessions": int(err_sessions),
            "sessions": sessions,
            "errorsPerSession": round(int(errors) / sessions, 4) if sessions else None,
            "firstSeenAt": _iso(first_seen),
            "lastSeenAt": _iso(last_seen),
        })
    # Most recently active release first — the latest deploy is what you're judging.
    releases.sort(key=lambda r: r["lastSeenAt"] or "", reverse=True)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "releases": releases,
        }
    }












