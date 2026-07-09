"""Monitoring alerts.

A scheduled evaluator looks at each launched product's recent signals and emails
the owner when something needs attention. Four triggers:

* ``new_issue``     — a new error group appeared
* ``error_spike``   — today's error count is well above the recent baseline
* ``signups_drop``  — signups fell sharply week-over-week
* ``revenue_drop``  — MRR dropped versus the previous sync

Recipient is the product owner's account email (looked up from Firebase), so
there's no setup. Sends are best-effort and deduped via the Monitor alert table
so the same condition never emails twice. The deployed physical table still uses
its legacy ``portfolio_alerts`` name during the Phase 4 naming migration.

The decision logic (`detect_*`, `evaluate_alerts`) is pure and unit-tested; the
orchestrator is thin glue around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    MonitorProblem,
    Pipeline,
    MonitorAlert,
    MonitorAlertSettings,
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorRevenueSource,
    MonitorUsageEvent,
    MonitorUsageSource,
)
from app.services.email import email_service

logger = logging.getLogger(__name__)


# ── Effective preferences ────────────────────────────────────────────────────

@dataclass
class AlertPrefs:
    """Effective per-product alert settings: which triggers are on and the
    thresholds to use. Built by merging a product's saved row (if any) over the
    global config defaults; the anti-noise minimum floors always stay global."""

    new_issue_enabled: bool = True
    error_spike_enabled: bool = True
    signups_drop_enabled: bool = True
    revenue_drop_enabled: bool = True
    error_spike_multiplier: float = 3.0
    error_spike_min: int = 5
    signups_drop_pct: float = 0.5
    signups_min_previous: int = 5
    revenue_drop_pct: float = 0.2
    new_issue_cap: int = 5

    @classmethod
    def from_settings(cls, settings=None) -> "AlertPrefs":
        s = settings or get_settings()
        return cls(
            error_spike_multiplier=s.alert_error_spike_multiplier,
            error_spike_min=s.alert_error_spike_min,
            signups_drop_pct=s.alert_signups_drop_pct,
            signups_min_previous=s.alert_signups_min_previous,
            revenue_drop_pct=s.alert_revenue_drop_pct,
            new_issue_cap=s.alert_new_issue_cap,
        )

    @classmethod
    def resolve(cls, row: MonitorAlertSettings | None, settings=None) -> "AlertPrefs":
        base = cls.from_settings(settings)
        if row is None:
            return base
        # Coalesce booleans: a freshly created, not-yet-flushed row carries None
        # for columns whose defaults only apply on INSERT.
        def _on(value: bool | None) -> bool:
            return True if value is None else value

        return cls(
            new_issue_enabled=_on(row.new_issue_enabled),
            error_spike_enabled=_on(row.error_spike_enabled),
            signups_drop_enabled=_on(row.signups_drop_enabled),
            revenue_drop_enabled=_on(row.revenue_drop_enabled),
            error_spike_multiplier=row.error_spike_multiplier if row.error_spike_multiplier is not None else base.error_spike_multiplier,
            error_spike_min=base.error_spike_min,
            signups_drop_pct=row.signups_drop_pct if row.signups_drop_pct is not None else base.signups_drop_pct,
            signups_min_previous=base.signups_min_previous,
            revenue_drop_pct=row.revenue_drop_pct if row.revenue_drop_pct is not None else base.revenue_drop_pct,
            new_issue_cap=base.new_issue_cap,
        )


# ── Detection (pure) ────────────────────────────────────────────────────────

def detect_error_spike(current_errors: int, baseline_avg: float, *, min_errors: int, multiplier: float) -> bool:
    if current_errors < min_errors:
        return False
    if baseline_avg <= 0:
        # No baseline yet: a burst above the floor still counts.
        return True
    return current_errors >= baseline_avg * multiplier


def detect_signups_drop(current: int, previous: int, *, min_previous: int, drop_pct: float) -> bool:
    if previous < min_previous:
        return False
    return (previous - current) / previous >= drop_pct


def detect_revenue_drop(current_mrr: int | None, previous_mrr: int | None, *, drop_pct: float) -> bool:
    if not previous_mrr or previous_mrr <= 0 or current_mrr is None:
        return False
    return (previous_mrr - current_mrr) / previous_mrr >= drop_pct


def evaluate_alerts(snapshot: dict, prefs: AlertPrefs | None = None) -> list[dict]:
    """Given a product snapshot and its effective prefs, return the alerts that
    should fire. Each alert is ``{"type", "dedupe_key", "context"}``. Pure: no I/O.

    Disabled triggers are skipped entirely.
    """
    p = prefs or AlertPrefs.from_settings()
    product = snapshot.get("product_name") or "Your product"
    alerts: list[dict] = []

    # New error issues (cap a few per run so a burst can't blast inboxes).
    if p.new_issue_enabled:
        for group in (snapshot.get("new_issues") or [])[: p.new_issue_cap]:
            alerts.append({
                "type": "new_issue",
                "dedupe_key": str(group["id"]),
                "context": {"product": product, "title": group.get("title") or "New error", "level": group.get("level", "error")},
            })

    err = snapshot.get("error") or {}
    if p.error_spike_enabled and detect_error_spike(
        int(err.get("current", 0)), float(err.get("baseline", 0.0)),
        min_errors=p.error_spike_min, multiplier=p.error_spike_multiplier,
    ):
        alerts.append({
            "type": "error_spike",
            "dedupe_key": snapshot["today"],
            "context": {"product": product, "current": int(err.get("current", 0)), "baseline": round(float(err.get("baseline", 0.0)), 1)},
        })

    sign = snapshot.get("signups") or {}
    if p.signups_drop_enabled and detect_signups_drop(
        int(sign.get("current", 0)), int(sign.get("previous", 0)),
        min_previous=p.signups_min_previous, drop_pct=p.signups_drop_pct,
    ):
        alerts.append({
            "type": "signups_drop",
            "dedupe_key": snapshot["week"],
            "context": {"product": product, "current": int(sign.get("current", 0)), "previous": int(sign.get("previous", 0))},
        })

    rev = snapshot.get("revenue") or {}
    if p.revenue_drop_enabled and detect_revenue_drop(rev.get("current_mrr"), rev.get("previous_mrr"), drop_pct=p.revenue_drop_pct):
        alerts.append({
            "type": "revenue_drop",
            "dedupe_key": snapshot["today"],
            "context": {"product": product, "current_mrr": rev.get("current_mrr"), "previous_mrr": rev.get("previous_mrr")},
        })

    return alerts


# type -> (title, severity, metric) for the durable problem record.
_PROBLEM_META = {
    "new_issue": ("New error issue", "warning", "errors"),
    "error_spike": ("Error spike", "critical", "errors"),
    "signups_drop": ("Signups dropped", "warning", "signups"),
    "revenue_drop": ("MRR dropped", "critical", "mrr"),
}


def problem_from_alert(alert: dict, now: datetime) -> dict:
    """Map a fired alert to monitoring_problem fields. Pure. baseline/observed
    capture the signal that tripped so the impact view can frame before/after."""
    kind = alert["type"]
    ctx = alert.get("context") or {}
    title, severity, metric = _PROBLEM_META.get(kind, (kind, "warning", None))
    baseline = observed = None
    detail = None
    if kind == "error_spike":
        observed = float(ctx.get("current", 0))
        baseline = float(ctx.get("baseline", 0))
        detail = f"{ctx.get('current')} errors vs {ctx.get('baseline')} baseline"
    elif kind == "signups_drop":
        observed = float(ctx.get("current", 0))
        baseline = float(ctx.get("previous", 0))
        detail = f"{ctx.get('current')} signups vs {ctx.get('previous')} the prior period"
    elif kind == "revenue_drop":
        observed = float(ctx.get("current_mrr") or 0)
        baseline = float(ctx.get("previous_mrr") or 0)
        detail = f"MRR {ctx.get('current_mrr')} vs {ctx.get('previous_mrr')}"
    elif kind == "new_issue":
        title = ctx.get("title") or title
        detail = f"New {ctx.get('level', 'error')} issue"
    return {
        "kind": kind,
        "dedupe_key": alert["dedupe_key"],
        "title": title,
        "detail": detail,
        "severity": severity,
        "metric": metric,
        "baseline": baseline,
        "observed": observed,
        "detected_at": now,
    }


async def alert_settings_row(db: AsyncSession, pipeline_id: str) -> MonitorAlertSettings | None:
    return (await db.execute(
        select(MonitorAlertSettings).where(MonitorAlertSettings.pipeline_id == pipeline_id)
    )).scalar_one_or_none()


# ── Rendering ───────────────────────────────────────────────────────────────

def _money(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.0f}"


def render_alert(alert_type: str, context: dict) -> tuple[str, str, str]:
    product = context.get("product", "Your product")
    if alert_type == "new_issue":
        subject = f"[{product}] New error: {context['title']}"
        body = f"A new {context.get('level', 'error')} appeared in {product}:\n\n{context['title']}"
    elif alert_type == "error_spike":
        subject = f"[{product}] Error spike: {context['current']} errors today"
        body = f"{product} logged {context['current']} errors today, well above its recent average of {context['baseline']}/day."
    elif alert_type == "signups_drop":
        subject = f"[{product}] Signups dropped this week"
        body = f"{product} had {context['current']} signups this week, down from {context['previous']} the week before."
    elif alert_type == "revenue_drop":
        subject = f"[{product}] MRR dropped"
        body = f"{product} MRR fell from {_money(context['previous_mrr'])} to {_money(context['current_mrr'])}."
    else:  # pragma: no cover - defensive
        subject = f"[{product}] Monitor alert"
        body = "A monitored condition was triggered."
    html = f"<p>{body}</p>"
    return subject, body, html


# ── Recipient ───────────────────────────────────────────────────────────────

def resolve_owner_email(uid: str) -> str | None:
    """The product owner's account email from Firebase, or None if unavailable."""
    try:
        from firebase_admin import auth
        user = auth.get_user(uid)
        email = getattr(user, "email", None)
        return email if isinstance(email, str) and "@" in email else None
    except Exception as exc:  # pragma: no cover - depends on firebase
        logger.warning("could not resolve owner email for %s: %s", uid, exc)
        return None


# ── Dedupe ──────────────────────────────────────────────────────────────────

async def already_alerted(db: AsyncSession, pipeline_id: str, alert_type: str, dedupe_key: str) -> bool:
    existing = (await db.execute(
        select(MonitorAlert.id).where(
            MonitorAlert.pipeline_id == pipeline_id,
            MonitorAlert.alert_type == alert_type,
            MonitorAlert.dedupe_key == dedupe_key,
        )
    )).scalar_one_or_none()
    return existing is not None


# ── Snapshot gathering + orchestration ──────────────────────────────────────

async def _gather_snapshot(pipeline_id: str, user_id: str, product_name: str, db: AsyncSession, now: datetime, settings=None) -> dict:
    s = settings or get_settings()
    baseline_days = s.alert_error_baseline_days
    signups_window = s.alert_signups_window_days
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=signups_window)
    prev_week = now - timedelta(days=2 * signups_window)
    # Baseline spans the `baseline_days` ending yesterday (today is the spike day).
    baseline_start = now - timedelta(days=1 + baseline_days)

    new_issues = list((await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.first_seen_at >= day_ago,
        )
    )).scalars().all())

    current_errors = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= day_ago,
        )
    ) or 0)
    prior_errors = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= baseline_start,
            MonitorErrorEvent.occurred_at < day_ago,
        )
    ) or 0)

    this_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
            MonitorUsageEvent.occurred_at >= week_ago,
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
            MonitorUsageEvent.occurred_at >= prev_week,
            MonitorUsageEvent.occurred_at < week_ago,
        )
    ) or 0)

    revenue = await db.scalar(
        select(MonitorRevenueSource).where(
            MonitorRevenueSource.pipeline_id == pipeline_id,
            MonitorRevenueSource.provider == "stripe",
        )
    )

    return {
        "pipeline_id": pipeline_id,
        "user_id": user_id,
        "product_name": product_name,
        "today": now.date().isoformat(),
        "week": f"{now.isocalendar().year}-W{now.isocalendar().week:02d}",
        "new_issues": [{"id": g.id, "title": g.title, "level": g.level} for g in new_issues],
        "error": {"current": current_errors, "baseline": prior_errors / baseline_days},
        "signups": {"current": this_week_signups, "previous": prev_week_signups},
        "revenue": {
            "current_mrr": revenue.current_mrr_cents if revenue else None,
            "previous_mrr": revenue.previous_mrr_cents if revenue else None,
        },
    }


async def _monitored_products(db: AsyncSession) -> list[tuple[str, str]]:
    """(pipeline_id, owner_uid) for every launched product with monitoring."""
    rows = (await db.execute(
        select(Pipeline.id, Pipeline.user_id)
        .join(MonitorUsageSource, MonitorUsageSource.pipeline_id == Pipeline.id)
        .where(Pipeline.launched_at != None)
        .distinct()
    )).all()
    return [(pid, uid) for pid, uid in rows]


async def run_alert_checks() -> int:
    """Scheduler entrypoint: evaluate every monitored product and email owners."""
    from app.db import AsyncSessionLocal
    from app.services.app_settings import effective_config
    from app.services.preferences import NotificationPrefs, notification_prefs_for

    settings = get_settings()
    sent = 0
    async with AsyncSessionLocal() as db:
        # Overlay admin overrides (alerts switch, baseline days, new-issue cap)
        # on top of the config defaults.
        cfg = await effective_config(db, settings)
        if not cfg.alerts_enabled:
            return 0
        now = datetime.now(timezone.utc)
        products = await _monitored_products(db)
        # Map pipeline_id -> name for nicer subjects.
        names = dict((row[0], row[1]) for row in (await db.execute(
            select(Pipeline.id, Pipeline.name)
        )).all())

        prefs_cache: dict[str, NotificationPrefs] = {}
        for pipeline_id, uid in products:
            try:
                np = prefs_cache.get(uid)
                if np is None:
                    np = await notification_prefs_for(db, uid)
                    prefs_cache[uid] = np
                aprefs = AlertPrefs.resolve(await alert_settings_row(db, pipeline_id), cfg)
                snapshot = await _gather_snapshot(pipeline_id, uid, names.get(pipeline_id, "Your product"), db, now, cfg)
                # Resolve the destination once; None when the user has turned alert
                # emails off. Digest cadences record now and email from the digest job.
                recipient = (np.alert_email or resolve_owner_email(uid)) if np.alerts_email_enabled else None
                for alert in evaluate_alerts(snapshot, aprefs):
                    if await already_alerted(db, pipeline_id, alert["type"], alert["dedupe_key"]):
                        continue
                    # Record the durable problem before emailing: the condition
                    # is an inspectable object even when alert email is off.
                    db.add(MonitorProblem(pipeline_id=pipeline_id, **problem_from_alert(alert, now)))
                    if np.send_instantly and recipient:
                        subject, text, html = render_alert(alert["type"], alert["context"])
                        await email_service.send(to=recipient, subject=subject, text=text, html=html)
                        sent += 1
                    db.add(MonitorAlert(
                        pipeline_id=pipeline_id,
                        alert_type=alert["type"],
                        dedupe_key=alert["dedupe_key"],
                        recipient=recipient,
                    ))
                    await db.commit()
            except Exception as exc:
                logger.warning("alert check failed for product %s: %s", pipeline_id, exc)

    logger.info("alert checks complete: %s email(s) sent", sent)
    return sent


# ── Digest (daily/weekly summary) ────────────────────────────────────────────

ALERT_LABELS = {
    "new_issue": "New error issue",
    "error_spike": "Error spike",
    "signups_drop": "Signups dropped",
    "revenue_drop": "MRR dropped",
}


def render_digest(items: list[dict], cadence: str) -> tuple[str, str, str]:
    """Render a digest of recorded alerts. Pure: ``items`` is a list of
    ``{"type", "product"}``. Returns (subject, text, html)."""
    period = "daily" if cadence == "daily" else "weekly"
    count = len(items)
    subject = f"[Monitor] Your {period} alert digest — {count} alert{'s' if count != 1 else ''}"

    by_product: dict[str, list[str]] = {}
    for it in items:
        by_product.setdefault(it.get("product") or "Your product", []).append(it.get("type", "alert"))

    text_parts = [f"Your {period} summary of monitoring alerts:"]
    html_parts = [f"<p>Your {period} summary of monitoring alerts:</p>"]
    for product, types in by_product.items():
        text_parts.append(f"\n{product}:")
        html_parts.append(f"<p><strong>{product}</strong></p><ul>")
        for t in types:
            label = ALERT_LABELS.get(t, t)
            text_parts.append(f"  - {label}")
            html_parts.append(f"<li>{label}</li>")
        html_parts.append("</ul>")
    return subject, "\n".join(text_parts), "".join(html_parts)


async def run_alert_digests() -> int:
    """Scheduler entrypoint: email a summary to users on a daily/weekly cadence
    whose digest interval has elapsed, covering alerts recorded since last time."""
    from app.db import AsyncSessionLocal
    from app.models import UserPreference
    from app.services.app_settings import effective_config
    from app.services.preferences import digest_interval

    settings = get_settings()
    sent = 0
    async with AsyncSessionLocal() as db:
        cfg = await effective_config(db, settings)
        if not cfg.alerts_enabled:
            return 0
        now = datetime.now(timezone.utc)
        prefs = list((await db.execute(
            select(UserPreference).where(
                UserPreference.digest_cadence.in_(("daily", "weekly")),
                UserPreference.alerts_email_enabled == True,  # noqa: E712
            )
        )).scalars().all())

        for pref in prefs:
            try:
                interval = digest_interval(pref.digest_cadence)
                if interval is None:
                    continue
                if pref.last_digest_sent_at and (now - pref.last_digest_sent_at) < interval:
                    continue
                since = pref.last_digest_sent_at or (now - interval)
                rows = (await db.execute(
                    select(MonitorAlert.alert_type, Pipeline.name, MonitorAlert.created_at)
                    .join(Pipeline, Pipeline.id == MonitorAlert.pipeline_id)
                    .where(Pipeline.user_id == pref.uid, MonitorAlert.created_at >= since)
                    .order_by(MonitorAlert.created_at.asc())
                )).all()
                if rows:
                    recipient = pref.alert_email or resolve_owner_email(pref.uid)
                    if recipient:
                        items = [{"type": r[0], "product": r[1]} for r in rows]
                        subject, text, html = render_digest(items, pref.digest_cadence)
                        await email_service.send(to=recipient, subject=subject, text=text, html=html)
                        sent += 1
                pref.last_digest_sent_at = now
                await db.commit()
            except Exception as exc:
                logger.warning("alert digest failed for user %s: %s", pref.uid, exc)

    logger.info("alert digests complete: %s email(s) sent", sent)
    return sent
