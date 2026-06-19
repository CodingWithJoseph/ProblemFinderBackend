"""Monitoring alerts.

A scheduled evaluator looks at each launched product's recent signals and emails
the owner when something needs attention. Four triggers:

* ``new_issue``     — a new error group appeared
* ``error_spike``   — today's error count is well above the recent baseline
* ``signups_drop``  — signups fell sharply week-over-week
* ``revenue_drop``  — MRR dropped versus the previous sync

Recipient is the product owner's account email (looked up from Firebase), so
there's no setup. Sends are best-effort and deduped via the ``portfolio_alerts``
table so the same condition never emails twice.

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
    Pipeline,
    PortfolioAlert,
    PortfolioAlertSettings,
    PortfolioErrorEvent,
    PortfolioErrorGroup,
    PortfolioRevenueSource,
    PortfolioUsageEvent,
    PortfolioUsageSource,
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
    def resolve(cls, row: PortfolioAlertSettings | None, settings=None) -> "AlertPrefs":
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


async def alert_settings_row(db: AsyncSession, pipeline_id: str) -> PortfolioAlertSettings | None:
    return (await db.execute(
        select(PortfolioAlertSettings).where(PortfolioAlertSettings.pipeline_id == pipeline_id)
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
        select(PortfolioAlert.id).where(
            PortfolioAlert.pipeline_id == pipeline_id,
            PortfolioAlert.alert_type == alert_type,
            PortfolioAlert.dedupe_key == dedupe_key,
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
        select(PortfolioErrorGroup).where(
            PortfolioErrorGroup.pipeline_id == pipeline_id,
            PortfolioErrorGroup.first_seen_at >= day_ago,
        )
    )).scalars().all())

    current_errors = int(await db.scalar(
        select(func.count(PortfolioErrorEvent.id)).where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= day_ago,
        )
    ) or 0)
    prior_errors = int(await db.scalar(
        select(func.count(PortfolioErrorEvent.id)).where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= baseline_start,
            PortfolioErrorEvent.occurred_at < day_ago,
        )
    ) or 0)

    this_week_signups = int(await db.scalar(
        select(func.count(PortfolioUsageEvent.id)).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.event_type == "signup",
            PortfolioUsageEvent.occurred_at >= week_ago,
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(PortfolioUsageEvent.id)).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.event_type == "signup",
            PortfolioUsageEvent.occurred_at >= prev_week,
            PortfolioUsageEvent.occurred_at < week_ago,
        )
    ) or 0)

    revenue = await db.scalar(
        select(PortfolioRevenueSource).where(
            PortfolioRevenueSource.pipeline_id == pipeline_id,
            PortfolioRevenueSource.provider == "stripe",
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
        .join(PortfolioUsageSource, PortfolioUsageSource.pipeline_id == Pipeline.id)
        .where(Pipeline.launched_at != None)
        .distinct()
    )).all()
    return [(pid, uid) for pid, uid in rows]


async def run_alert_checks() -> int:
    """Scheduler entrypoint: evaluate every monitored product and email owners."""
    settings = get_settings()
    if not settings.alerts_enabled:
        return 0

    from app.db import AsyncSessionLocal

    sent = 0
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        products = await _monitored_products(db)
        # Map pipeline_id -> name for nicer subjects.
        names = dict((row[0], row[1]) for row in (await db.execute(
            select(Pipeline.id, Pipeline.name)
        )).all())

        for pipeline_id, uid in products:
            try:
                prefs = AlertPrefs.resolve(await alert_settings_row(db, pipeline_id), settings)
                snapshot = await _gather_snapshot(pipeline_id, uid, names.get(pipeline_id, "Your product"), db, now, settings)
                for alert in evaluate_alerts(snapshot, prefs):
                    if await already_alerted(db, pipeline_id, alert["type"], alert["dedupe_key"]):
                        continue
                    recipient = resolve_owner_email(uid)
                    if recipient:
                        subject, text, html = render_alert(alert["type"], alert["context"])
                        await email_service.send(to=recipient, subject=subject, text=text, html=html)
                        sent += 1
                    db.add(PortfolioAlert(
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
