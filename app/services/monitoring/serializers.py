from datetime import datetime, timezone

from app.models import (
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorInvestigation,
    MonitorInvestigationEntry,
    MonitorProblem,
    MonitorReport,
    MonitorSpan,
    MonitorUsageEvent,
    MonitorUsageSource,
)
from app.services.alerts import AlertPrefs
from app.services.monitoring.common import _iso


def _serialize_usage_source(source: MonitorUsageSource | None) -> dict | None:
    if not source:
        return None
    return {
        "id": source.id,
        "pipelineId": source.pipeline_id,
        "publicKey": source.public_key,
        "name": source.name,
        "status": source.status,
        "productUrl": source.product_url,
        "allowedDomain": source.allowed_domain,
        "createdAt": _iso(source.created_at),
        "updatedAt": _iso(source.updated_at),
        "lastSeenAt": _iso(source.last_seen_at),
    }


def _serialize_usage_event(event: MonitorUsageEvent) -> dict:
    return {
        "id": event.id,
        "pipelineId": event.pipeline_id,
        "sourceId": event.source_id,
        "eventType": event.event_type,
        "visitorId": event.visitor_id,
        "sessionId": event.session_id,
        "userId": event.user_ref,
        "url": event.url,
        "referrer": event.referrer,
        "release": event.release,
        "environment": event.environment,
        "metadata": event.event_metadata or {},
        "occurredAt": _iso(event.occurred_at),
        "receivedAt": _iso(event.received_at),
    }


def _serialize_error_group(group: MonitorErrorGroup, affected_sessions: int | None = None) -> dict:
    payload = {
        "id": group.id,
        "pipelineId": group.pipeline_id,
        "fingerprint": group.fingerprint,
        "title": group.title,
        "level": group.level,
        "status": group.status,
        "errorType": group.error_type,
        "eventCount": int(group.event_count or 0),
        "lastRelease": group.last_release,
        "firstSeenAt": _iso(group.first_seen_at),
        "lastSeenAt": _iso(group.last_seen_at),
    }
    if affected_sessions is not None:
        payload["affectedSessions"] = int(affected_sessions)
    return payload


def _serialize_error_event(event: MonitorErrorEvent) -> dict:
    return {
        "id": event.id,
        "pipelineId": event.pipeline_id,
        "groupId": event.group_id,
        "fingerprint": event.fingerprint,
        "message": event.message,
        "stack": event.stack,
        "level": event.level,
        "handled": event.handled,
        "errorType": event.error_type,
        "platform": event.platform,
        "url": event.url,
        "release": event.release,
        "environment": event.environment,
        "traceId": event.trace_id,
        "spanId": event.span_id,
        "visitorId": event.visitor_id,
        "sessionId": event.session_id,
        "userId": event.user_ref,
        "metadata": event.event_metadata or {},
        "occurredAt": _iso(event.occurred_at),
        "receivedAt": _iso(event.received_at),
    }


def _serialize_span(span: MonitorSpan, depth: int = 0) -> dict:
    return {
        "id": span.id,
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "parentSpanId": span.parent_span_id,
        "name": span.name,
        "kind": span.kind,
        "service": span.service,
        "feature": span.feature,
        "platform": span.platform,
        "status": span.status,
        "release": span.release,
        "environment": span.environment,
        "durationMs": span.duration_ms,
        "startAt": _iso(span.start_at),
        "attributes": span.attributes or {},
        "depth": depth,
    }


def _order_span_tree(spans: list) -> list[tuple]:
    """Preorder spans into a parent→child tree with depth. A span whose parent
    isn't in the trace (e.g. a server span whose client parent was sampled out)
    is treated as a root. Visited-guarded so a malformed parent cycle can't loop,
    and any span not reached from a root is appended so nothing is dropped."""
    ids = {s.span_id for s in spans}
    by_parent: dict = {}
    for s in spans:
        key = s.parent_span_id if s.parent_span_id in ids else None
        by_parent.setdefault(key, []).append(s)
    for children in by_parent.values():
        children.sort(key=lambda s: s.start_at or datetime.min.replace(tzinfo=timezone.utc))

    ordered: list[tuple] = []
    visited: set = set()

    def walk(parent_key, depth):
        for s in by_parent.get(parent_key, []):
            if s.span_id in visited:
                continue
            visited.add(s.span_id)
            ordered.append((s, depth))
            walk(s.span_id, depth + 1)

    walk(None, 0)
    for s in spans:
        if s.span_id not in visited:
            ordered.append((s, 0))
    return ordered


def _serialize_problem(problem: MonitorProblem) -> dict:
    return {
        "id": problem.id,
        "kind": problem.kind,
        "title": problem.title,
        "detail": problem.detail,
        "severity": problem.severity,
        "status": problem.status,
        "metric": problem.metric,
        "baseline": problem.baseline,
        "observed": problem.observed,
        "detectedAt": _iso(problem.detected_at),
        "resolvedAt": _iso(problem.resolved_at),
    }


def _serialize_investigation(inv: MonitorInvestigation) -> dict:
    return {
        "id": inv.id,
        "title": inv.title,
        "summary": inv.summary,
        "status": inv.status,
        "createdAt": _iso(inv.created_at),
        "updatedAt": _iso(inv.updated_at),
    }


def _serialize_entry(entry: MonitorInvestigationEntry) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "refId": entry.ref_id,
        "body": entry.body,
        "metadata": entry.event_metadata or {},
        "createdAt": _iso(entry.created_at),
    }


def _serialize_report(report: MonitorReport) -> dict:
    return {
        "id": report.id,
        "title": report.title,
        "body": report.body,
        "investigationId": report.investigation_id,
        "createdAt": _iso(report.created_at),
        "updatedAt": _iso(report.updated_at),
    }


def _serialize_alert_settings(prefs: AlertPrefs) -> dict:
    return {
        "newIssueEnabled": prefs.new_issue_enabled,
        "errorSpikeEnabled": prefs.error_spike_enabled,
        "signupsDropEnabled": prefs.signups_drop_enabled,
        "revenueDropEnabled": prefs.revenue_drop_enabled,
        "errorSpikeMultiplier": prefs.error_spike_multiplier,
        "signupsDropPct": prefs.signups_drop_pct,
        "revenueDropPct": prefs.revenue_drop_pct,
    }


def _serialize_issue(group: MonitorErrorGroup, *, affected_users, affected_sessions, occurrences, trend) -> dict:
    """A fingerprint group promoted to an issue object: the group's identity plus
    the impact metrics the engine computes (affected users/sessions, windowed
    occurrences, trend). affected-users is net-new — it isn't stored anywhere."""
    return {
        "id": group.id,
        "fingerprint": group.fingerprint,
        "title": group.title,
        "level": group.level,
        "status": group.status,
        "errorType": group.error_type,
        "lastRelease": group.last_release,
        "firstSeenAt": _iso(group.first_seen_at),
        "lastSeenAt": _iso(group.last_seen_at),
        "totalOccurrences": int(group.event_count or 0),
        "occurrences": int(occurrences),
        "affectedUsers": int(affected_users),
        "affectedSessions": int(affected_sessions),
        "trend": trend,
    }
