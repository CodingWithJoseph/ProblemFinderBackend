"""Pipeline card serialization.

Pure helpers that turn a Pipeline row (+ its posts/team/issue counts) into the
JSON card shape the API returns. Extracted from routes/pipeline.py so both the
pipeline and portfolio routers share one serializer.
"""

from app.models import Pipeline


def format_cluster_metrics(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "avgSourceScore": raw.get("avg_source_score"),
        "coherenceScore": raw.get("coherence_score"),
        "postCount": raw.get("post_count"),
        "computedAt": raw.get("computed_at"),
    }


def serialize_card(
    p: Pipeline,
    posts: list[dict] | None = None,
    team: dict | None = None,
    open_issue_count: int = 0,
    open_kill_criteria_count: int = 0,
) -> dict:
    return {
        "id": p.id,
        "userId": p.user_id,
        "teamId": p.team_id,
        "team": team,
        "name": p.name,
        "projectName": p.project_name,
        "iconUrl": p.icon_url,
        # Effective title shown everywhere: the user-given name, falling back to
        # the cluster name when the project hasn't been renamed.
        "displayName": p.project_name or p.name,
        "timelineDays": p.timeline_days,
        "timelineStart": p.timeline_start.isoformat() if p.timeline_start else None,
        "timelineTargetLaunch": p.timeline_target_launch.isoformat() if p.timeline_target_launch else None,
        "postIds": p.post_ids or [],
        "sourceClusterId": p.source_cluster_id,
        "stage": p.stage,
        "killCriteria": p.kill_criteria,
        "distributionChannels": p.distribution_channels or [],
        "conceptAngles": p.concept_angles or [],
        "exitChecklist": p.exit_checklist,
        "notes": p.notes,
        "url": p.url,
        "category": p.category,
        "revenueModel": p.revenue_model,
        "status": p.status,
        "clusterMetrics": format_cluster_metrics(p.cluster_metrics),
        "outcome": p.outcome,
        "mrr": p.mrr,
        "outcomeNotedAt": p.outcome_noted_at.isoformat() if p.outcome_noted_at else None,
        "launchedAt": p.launched_at.isoformat() if p.launched_at else None,
        "removedAt": p.removed_at.isoformat() if p.removed_at else None,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
        "openIssueCount": open_issue_count,
        "openKillCriteriaCount": open_kill_criteria_count,
        "posts": posts or [],
    }


def serialize_cards(
    cards: list[Pipeline],
    posts_map: dict[str, list[dict]],
    teams_map: dict[str, dict],
    counts_map: dict[str, dict[str, int]],
) -> list[dict]:
    return [
        serialize_card(
            c,
            posts=posts_map.get(str(c.id), []),
            team=teams_map.get(str(c.team_id)) if c.team_id else None,
            open_issue_count=counts_map.get(str(c.id), {}).get("issue", 0),
            open_kill_criteria_count=counts_map.get(str(c.id), {}).get("kill_criteria", 0),
        )
        for c in cards
    ]
