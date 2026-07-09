from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MonitorErrorEvent, MonitorUsageEvent, MonitorWebVital
from app.services.monitoring.common import _as_date, _iso, _now, _pct_change
from app.services.monitoring.sources import _usage_source_is_connected


# Core Web Vitals thresholds (good upper-bound, poor lower-bound). LCP/INP/FCP/
# TTFB are milliseconds; CLS is unitless. A p75 at/below `good` is good, above
# `poor` is poor, between is needs-improvement.
_VITAL_THRESHOLDS = {
    "LCP": (2500.0, 4000.0),
    "INP": (200.0, 500.0),
    "FCP": (1800.0, 3000.0),
    "TTFB": (800.0, 1800.0),
    "CLS": (0.1, 0.25),
}
_VITAL_ORDER = {"LCP": 0, "INP": 1, "CLS": 2, "FCP": 3, "TTFB": 4}


def _health_verdict(*, connected: bool, last_seen_at, total_events: int, eff, now) -> dict:
    """Health verdict engine v0: a per-source computed state, so the UI renders a
    badge instead of making the user read a timestamp and guess. Freshness-only
    for now (errors/vitals/revenue fold in at v2). States: no-data / healthy /
    warning / unhealthy."""
    if not connected or last_seen_at is None or total_events <= 0:
        return {"state": "no-data", "label": "No data", "reason": "No events received yet",
                "lastSeenAt": _iso(last_seen_at), "ageHours": None}

    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - seen).total_seconds() / 3600.0)
    if age_hours <= eff.health_warning_hours:
        state, label, reason = "healthy", "Healthy", "Receiving events"
    elif age_hours <= eff.health_unhealthy_hours:
        state, label, reason = "warning", "Quiet", f"No events for {int(age_hours)}h"
    else:
        state, label, reason = "unhealthy", "Silent", f"No events for {int(age_hours)}h"
    return {"state": state, "label": label, "reason": reason,
            "lastSeenAt": _iso(last_seen_at), "ageHours": round(age_hours, 1)}


def _health_verdict_v2(*, connected, last_seen_at, total_events, error_rate, lcp_rating, eff, now) -> dict:
    """Health verdict engine v2: freshness + error rate + vitals → a richer
    operational state. Precedence is severity-ordered: nothing to judge, then
    gone silent, then failing/noisy on live data, then merely stale, else live."""
    def verdict(state, label, reason, age_hours):
        return {"state": state, "label": label, "reason": reason,
                "lastSeenAt": _iso(last_seen_at), "ageHours": round(age_hours, 1) if age_hours is not None else None}

    if not connected or last_seen_at is None or total_events <= 0:
        return verdict("no-data", "No data", "No events received yet", None)

    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - seen).total_seconds() / 3600.0)

    if age_hours > eff.health_unhealthy_hours:
        return verdict("silent", "Silent", f"No events for {int(age_hours)}h", age_hours)

    rate_pct = f"{round(error_rate * 100)}%" if error_rate is not None else None
    if (error_rate is not None and error_rate >= eff.health_error_rate_failing):
        return verdict("failing", "Failing", f"Error rate {rate_pct}", age_hours)
    if lcp_rating == "poor":
        return verdict("failing", "Failing", "Poor experience (LCP)", age_hours)
    if (error_rate is not None and error_rate >= eff.health_error_rate_noisy):
        return verdict("noisy", "Noisy", f"Error rate {rate_pct}", age_hours)
    if lcp_rating == "needs-improvement":
        return verdict("noisy", "Noisy", "Experience needs improvement (LCP)", age_hours)
    if age_hours > eff.health_warning_hours:
        return verdict("stale", "Stale", f"No events for {int(age_hours)}h", age_hours)
    return verdict("live", "Live", "Healthy and current", age_hours)


async def _source_health(pipeline_id: str, source, eff, now, db) -> tuple[dict, dict]:
    """Gather the signals the v2 verdict needs (freshness, error rate, LCP p75)
    and compute it. Returns (verdict, signals)."""
    last_seen = source.last_seen_at if source else None
    since = now - timedelta(days=eff.analytics_usage_window_days)

    total_events = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(MonitorUsageEvent.pipeline_id == pipeline_id)
    ) or 0)
    errors = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0)
    sessions = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.session_id != None,
        )
    ) or 0)
    lcp_p75 = await db.scalar(
        select(func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc())).where(
            MonitorWebVital.pipeline_id == pipeline_id,
            MonitorWebVital.occurred_at >= since,
            MonitorWebVital.metric == "LCP",
        )
    )
    error_rate = round(errors / sessions, 4) if sessions else None
    lcp_rating = _rate_vital("LCP", lcp_p75) if lcp_p75 is not None else None

    verdict = _health_verdict_v2(
        connected=_usage_source_is_connected(source, pipeline_id),
        last_seen_at=last_seen,
        total_events=total_events,
        error_rate=error_rate,
        lcp_rating=lcp_rating,
        eff=eff,
        now=now,
    )
    signals = {
        "errors": errors,
        "sessions": sessions,
        "errorRate": error_rate,
        "lcpP75": _round_vital("LCP", lcp_p75),
        "lcpRating": lcp_rating,
    }
    return verdict, signals


def _empty_error_daily(days: int = 14) -> dict[str, dict]:
    today = _now().date()
    rows: dict[str, dict] = {}
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        rows[day.isoformat()] = {"date": day.isoformat(), "errors": 0}
    return rows


def _empty_daily(days: int = 14) -> dict[str, dict]:
    today = _now().date()
    rows: dict[str, dict] = {}
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        rows[day.isoformat()] = {
            "date": day.isoformat(),
            "pageviews": 0,
            "visitors": 0,
            "signups": 0,
            "activations": 0,
            "events": 0,
        }
    return rows


def _compute_retention(
    first_seen_rows: list[tuple],
    active_rows: list[tuple],
    today: date,
    window_days: int = 30,
    cohort_limit: int = 7,
) -> dict:
    """Day-1 / Day-7 return retention.

    A visitor is "retained DN" if they have any activity N days after their
    first-seen day. Only cohorts old enough to have been observed for N days
    count toward the rate, so a brand-new cohort doesn't drag it down.
    """
    active = {(visitor, _as_date(day)) for visitor, day in active_rows}
    overall = {1: {"eligible": 0, "retained": 0}, 7: {"eligible": 0, "retained": 0}}
    cohorts: dict[date, dict] = {}

    for visitor, day in first_seen_rows:
        first = _as_date(day)
        cohort = cohorts.setdefault(first, {"size": 0, 1: {"eligible": 0, "retained": 0}, 7: {"eligible": 0, "retained": 0}})
        cohort["size"] += 1
        for n in (1, 7):
            if first + timedelta(days=n) <= today:
                overall[n]["eligible"] += 1
                cohort[n]["eligible"] += 1
                if (visitor, first + timedelta(days=n)) in active:
                    overall[n]["retained"] += 1
                    cohort[n]["retained"] += 1

    def _bucket(agg: dict) -> dict:
        rate = round(agg["retained"] / agg["eligible"], 4) if agg["eligible"] else None
        return {"eligible": agg["eligible"], "retained": agg["retained"], "rate": rate}

    def _rate(agg: dict) -> float | None:
        return round(agg["retained"] / agg["eligible"], 4) if agg["eligible"] else None

    cohort_list = [
        {
            "date": cohort_date.isoformat(),
            "size": cohorts[cohort_date]["size"],
            "d1Rate": _rate(cohorts[cohort_date][1]),
            "d7Rate": _rate(cohorts[cohort_date][7]),
        }
        for cohort_date in sorted(cohorts.keys(), reverse=True)[:cohort_limit]
    ]

    return {
        "windowDays": window_days,
        "d1": _bucket(overall[1]),
        "d7": _bucket(overall[7]),
        "cohorts": cohort_list,
    }


def _series_stats(values: list[int]) -> dict:
    """Normal-range band for the hero chart: mean +/- 1 stddev over the window
    (lower clamped at 0). A point outside the band is the 'this is abnormal' cue."""
    if not values:
        return {"mean": 0.0, "lower": 0.0, "upper": 0.0}
    n = len(values)
    mean = sum(values) / n
    std = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
    return {"mean": round(mean, 2), "lower": round(max(0.0, mean - std), 2), "upper": round(mean + std, 2)}


def _explorer_health(error_rate: float | None, lcp_rating: str | None, loads: int) -> str:
    """One-glance verdict per page: error rate dominates, then felt-speed."""
    if not loads:
        return "no-data"
    if error_rate is not None and error_rate >= 0.05:
        return "unhealthy"
    if lcp_rating == "poor" or (error_rate is not None and error_rate >= 0.01):
        return "warning"
    return "healthy"


def _flow_graph(rows, *, max_nodes: int = 30, max_edges: int = 50) -> tuple[list, list]:
    """Build the journey graph from pageviews ordered by (session, time): node =
    page with its visit count, edge = a consecutive page→page hop within one
    session (self-hops/refreshes dropped). Edges are kept only between the top
    nodes so the graph stays readable."""
    visits: dict[str, int] = {}
    edges: dict[tuple, int] = {}
    prev_session = None
    prev_url = None
    for session_id, url, _ in rows:
        visits[url] = visits.get(url, 0) + 1
        if session_id == prev_session and prev_url is not None and prev_url != url:
            key = (prev_url, url)
            edges[key] = edges.get(key, 0) + 1
        prev_session = session_id
        prev_url = url

    top = sorted(visits.items(), key=lambda kv: kv[1], reverse=True)[:max_nodes]
    node_set = {url for url, _ in top}
    nodes = [{"url": url, "visits": count} for url, count in top]
    edge_list = [
        {"from": a, "to": b, "count": count}
        for (a, b), count in sorted(edges.items(), key=lambda kv: kv[1], reverse=True)
        if a in node_set and b in node_set
    ][:max_edges]
    return nodes, edge_list


def _feature_flow(rows, *, max_nodes: int = 30, max_edges: int = 50) -> tuple[list, list]:
    """Build the flow graph from feature spans ordered by (session, time): node =
    a named feature (Sign Up, Checkout) with its run count, error count and mean
    duration; edge = a consecutive feature→feature hop within one session. Unlike
    the URL graph this measures outcomes — how often a flow ran, how slow it was,
    and how often it failed — so Monitor can show feature names, not pages."""
    stats: dict[str, dict] = {}
    edges: dict[tuple, int] = {}
    prev_session = None
    prev_feature = None
    for session_id, feature, status, duration_ms, _ in rows:
        stat = stats.setdefault(feature, {"count": 0, "errors": 0, "dur_sum": 0.0, "dur_n": 0})
        stat["count"] += 1
        if status == "error":
            stat["errors"] += 1
        if duration_ms is not None:
            stat["dur_sum"] += duration_ms
            stat["dur_n"] += 1
        if session_id == prev_session and prev_feature is not None and prev_feature != feature:
            key = (prev_feature, feature)
            edges[key] = edges.get(key, 0) + 1
        prev_session = session_id
        prev_feature = feature

    top = sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:max_nodes]
    node_set = {feature for feature, _ in top}
    nodes = [
        {
            "feature": feature,
            "count": stat["count"],
            "errorCount": stat["errors"],
            "avgDurationMs": round(stat["dur_sum"] / stat["dur_n"], 1) if stat["dur_n"] else None,
        }
        for feature, stat in top
    ]
    edge_list = [
        {"from": a, "to": b, "count": count}
        for (a, b), count in sorted(edges.items(), key=lambda kv: kv[1], reverse=True)
        if a in node_set and b in node_set
    ][:max_edges]
    return nodes, edge_list


def _round_vital(metric: str, value) -> float | None:
    if value is None:
        return None
    # CLS is a small unitless score; keep precision. The rest are milliseconds.
    return round(float(value), 3 if metric == "CLS" else 1)


def _build_correlation_days(usage_rows, error_rows, days: int = 30, today: date | None = None) -> list[dict]:
    """Merge usage + error daily rows into one aligned timeline."""
    today = today or _now().date()
    skeleton: dict[str, dict] = {}
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        skeleton[day.isoformat()] = {"date": day.isoformat(), "visitors": 0, "pageviews": 0, "signups": 0, "errors": 0}

    for day, event_type, count, visitors in usage_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key not in skeleton:
            continue
        if event_type == "pageview":
            skeleton[key]["pageviews"] += int(count or 0)
            skeleton[key]["visitors"] = max(skeleton[key]["visitors"], int(visitors or 0))
        elif event_type == "signup":
            skeleton[key]["signups"] += int(count or 0)

    for day, count in error_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key in skeleton:
            skeleton[key]["errors"] += int(count or 0)

    return list(skeleton.values())


def _correlation_insights(days: list[dict]) -> list[str]:
    """Plain-language callouts where signals move together — the cross-source read."""
    if not days:
        return []
    errors = [d["errors"] for d in days]
    signups = [d["signups"] for d in days]
    avg_err = sum(errors) / len(errors) if errors else 0
    avg_signups = sum(signups) / len(signups) if signups else 0

    insights: list[str] = []
    for day in reversed(days):  # most recent first
        if day["errors"] >= 5 and avg_err > 0 and day["errors"] >= 2 * avg_err:
            msg = f"Errors spiked to {day['errors']} on {day['date']}"
            if day["signups"] <= avg_signups:
                msg += f", while signups dipped to {day['signups']}"
            insights.append(msg + ".")
        if len(insights) >= 3:
            break
    return insights


def _issue_trend(recent: int, prior: int) -> dict:
    """Direction of an issue over the window: this half vs the prior half. 'up'
    means it's getting worse (a regression signal), 'down' means it's receding."""
    if recent > prior:
        direction = "up"
    elif recent < prior:
        direction = "down"
    else:
        direction = "flat"
    change_pct = _pct_change(recent, prior)
    return {"direction": direction, "recent": int(recent), "prior": int(prior), "changePct": change_pct}


def _rate_vital(metric: str, value: float | None) -> str | None:
    thresholds = _VITAL_THRESHOLDS.get(metric)
    if not thresholds or value is None:
        return None
    good, poor = thresholds
    if value <= good:
        return "good"
    if value <= poor:
        return "needs-improvement"
    return "poor"
