"""Goal Progress Engine.

Reads existing rollups/flags (never the raw event firehose), compares each goal's
current value to the next un-achieved tier per project+goal or account+goal, and
inserts an achievement row when a tier is crossed — emitting ``goal_tier_achieved``.
Achievement rows are the durable, append-only milestone log the UI renders.

The engine is idempotent: achievement inserts use ON CONFLICT DO NOTHING against
the (project|user, goal, tier) unique key, so re-running never double-records.
It is invoked on read (the goals endpoints reconcile before returning) and can be
called from the scheduled syncs.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccountGoalAchievement,
    GoalDefinition,
    GoalTier,
    Issue,
    MonitorErrorEvent,
    MonitorRevenueSource,
    MonitorUsageEvent,
    MonitorUsageSource,
    Pipeline,
    Problem,
    ProjectGoalAchievement,
    Task,
    Team,
    TeamMember,
)
from app.services.app_settings import effective_config

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _selected_mrr_cents(source: MonitorRevenueSource | None, engine: str) -> int:
    """Current MRR in cents for the configured revenue engine (mirrors the
    portfolio route's _selected_mrr without importing the routes layer)."""
    if not source:
        return 0
    if engine == "invoice":
        return int(source.invoice_mrr_cents or 0)
    return int(source.current_mrr_cents or 0)


async def _project_setup_steps(pipeline_id: str, db: AsyncSession) -> int:
    """Count the connections that make the Monitor section fully realized for a
    product (out of 4): a data source (usage beacon) connected, usage data
    flowing, revenue connected, and error monitoring active."""
    usage_source = await db.scalar(
        select(MonitorUsageSource).where(MonitorUsageSource.pipeline_id == pipeline_id).limit(1)
    )
    data_source_connected = bool(usage_source and usage_source.status == "connected")
    usage_flowing = bool(await db.scalar(
        select(MonitorUsageEvent.id).where(MonitorUsageEvent.pipeline_id == pipeline_id).limit(1)
    ))
    revenue_source = await db.scalar(
        select(MonitorRevenueSource).where(MonitorRevenueSource.pipeline_id == pipeline_id).limit(1)
    )
    revenue_connected = bool(revenue_source and revenue_source.status == "connected")
    errors_flowing = bool(await db.scalar(
        select(MonitorErrorEvent.id).where(MonitorErrorEvent.pipeline_id == pipeline_id).limit(1)
    ))
    return sum([data_source_connected, usage_flowing, revenue_connected, errors_flowing])


async def project_metric_values(pipeline_id: str, db: AsyncSession) -> dict[str, int]:
    """Current value for every project-scoped metric_key."""
    eff = await effective_config(db)
    revenue_source = await db.scalar(
        select(MonitorRevenueSource).where(MonitorRevenueSource.pipeline_id == pipeline_id).limit(1)
    )
    signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
        )
    ) or 0)
    traffic = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "pageview",
        )
    ) or 0)
    issues_created = int(await db.scalar(
        select(func.count(Issue.id)).where(Issue.pipeline_id == pipeline_id)
    ) or 0)
    # Pre-launch journey metrics: the path toward launch, computed per project.
    problems_defined = int(await db.scalar(
        select(func.count(Problem.id)).where(Problem.pipeline_id == pipeline_id)
    ) or 0)
    features_defined = int(await db.scalar(
        select(func.count(Task.id)).where(Task.pipeline_id == pipeline_id)
    ) or 0)
    features_built = int(await db.scalar(
        select(func.count(Task.id)).where(Task.pipeline_id == pipeline_id, Task.status == "done")
    ) or 0)
    return {
        "signups": signups,
        "traffic": traffic,
        "issues_created": issues_created,
        "mrr_cents": _selected_mrr_cents(revenue_source, eff.revenue_engine),
        "setup_steps": await _project_setup_steps(pipeline_id, db),
        "problems_defined": problems_defined,
        "features_defined": features_defined,
        "features_built": features_built,
    }


async def account_metric_values(uid: str, db: AsyncSession) -> dict[str, int]:
    """Current value for every account-scoped metric_key."""
    products_launched = int(await db.scalar(
        select(func.count(Pipeline.id)).where(Pipeline.user_id == uid, Pipeline.launched_at != None)
    ) or 0)
    problems_created = int(await db.scalar(
        select(func.count(Problem.id)).where(Problem.user_id == uid)
    ) or 0)
    tasks_created = int(await db.scalar(
        select(func.count(Task.id)).where(Task.user_id == uid)
    ) or 0)
    owned_team_ids = select(Team.id).where(Team.owner_user_id == uid)
    teammates_invited = int(await db.scalar(
        select(func.count(TeamMember.id)).where(
            TeamMember.team_id.in_(owned_team_ids),
            TeamMember.status != "removed",
            or_(TeamMember.user_id == None, TeamMember.user_id != uid),
        )
    ) or 0)
    return {
        "products_launched": products_launched,
        "problems_created": problems_created,
        "tasks_created": tasks_created,
        "teammates_invited": teammates_invited,
    }


async def _definitions_with_tiers(scope: str, db: AsyncSession) -> list[tuple[GoalDefinition, list[GoalTier]]]:
    definitions = list((await db.execute(
        select(GoalDefinition).where(GoalDefinition.scope == scope).order_by(GoalDefinition.sort_order)
    )).scalars().all())
    if not definitions:
        return []
    tier_rows = list((await db.execute(
        select(GoalTier)
        .where(GoalTier.goal_definition_id.in_([d.id for d in definitions]))
        .order_by(GoalTier.tier_index)
    )).scalars().all())
    tiers_by_goal: dict[str, list[GoalTier]] = {}
    for tier in tier_rows:
        tiers_by_goal.setdefault(tier.goal_definition_id, []).append(tier)
    return [(d, tiers_by_goal.get(d.id, [])) for d in definitions]


def _newly_crossed_indices(value: int, tiers: list[GoalTier], already: set[int]) -> list[int]:
    """Tier indices whose threshold the current value meets and which aren't yet
    recorded — usually one, but several can cross at once (e.g. a bulk import)."""
    return [t.tier_index for t in tiers if value >= t.threshold_value and t.tier_index not in already]


async def evaluate_project_goals(pipeline_id: str, db: AsyncSession) -> int:
    """Reconcile a project's goal achievements. Returns the number of tiers newly
    recorded. Does not commit — the caller owns the transaction."""
    defs = await _definitions_with_tiers("project", db)
    if not defs:
        return 0
    values = await project_metric_values(pipeline_id, db)
    existing = {(row.goal_definition_id, row.tier_index) for row in (await db.execute(
        select(ProjectGoalAchievement).where(ProjectGoalAchievement.project_id == pipeline_id)
    )).scalars().all()}
    now = _now()
    recorded = 0
    for definition, tiers in defs:
        value = int(values.get(definition.metric_key, 0))
        already = {index for (gid, index) in existing if gid == definition.id}
        for tier_index in _newly_crossed_indices(value, tiers, already):
            await db.execute(
                pg_insert(ProjectGoalAchievement.__table__)
                .values(project_id=pipeline_id, goal_definition_id=definition.id, tier_index=tier_index, achieved_at=now)
                .on_conflict_do_nothing(index_elements=["project_id", "goal_definition_id", "tier_index"])
            )
            _emit_goal_tier_achieved("project", pipeline_id, definition, tier_index, value)
            recorded += 1
    return recorded


async def evaluate_account_goals(uid: str, db: AsyncSession) -> int:
    """Reconcile an account's portfolio-level goal achievements. Returns the
    number of tiers newly recorded. Does not commit."""
    defs = await _definitions_with_tiers("account", db)
    if not defs:
        return 0
    values = await account_metric_values(uid, db)
    existing = {(row.goal_definition_id, row.tier_index) for row in (await db.execute(
        select(AccountGoalAchievement).where(AccountGoalAchievement.user_id == uid)
    )).scalars().all()}
    now = _now()
    recorded = 0
    for definition, tiers in defs:
        value = int(values.get(definition.metric_key, 0))
        already = {index for (gid, index) in existing if gid == definition.id}
        for tier_index in _newly_crossed_indices(value, tiers, already):
            await db.execute(
                pg_insert(AccountGoalAchievement.__table__)
                .values(user_id=uid, goal_definition_id=definition.id, tier_index=tier_index, achieved_at=now)
                .on_conflict_do_nothing(index_elements=["user_id", "goal_definition_id", "tier_index"])
            )
            _emit_goal_tier_achieved("account", uid, definition, tier_index, value)
            recorded += 1
    return recorded


def _emit_goal_tier_achieved(scope: str, owner_id: str, definition: GoalDefinition, tier_index: int, value: int) -> None:
    """Emit the goal_tier_achieved signal. The durable record is the achievement
    row; this is the observable hook (structured log) other systems can wire to."""
    logger.info(
        "goal_tier_achieved scope=%s owner=%s goal=%s tier=%s value=%s",
        scope, owner_id, definition.id, tier_index, value,
    )


def _tier_estimate_days(threshold: int, prev_threshold: int, configured: int | None) -> int:
    """Configured duration for a tier, or a scale-aware fallback when unset.

    The fallback grows with the jump from the previous threshold (in decades), so
    a 10x milestone jump is months out while a small early jump is close — never
    evenly spaced. Configured values (seeded per tier) always win."""
    if configured is not None:
        return int(configured)
    ratio = threshold / max(prev_threshold, 1)
    decades = math.log10(ratio) if ratio > 1 else 0.0
    return max(7, round(30 * max(decades, 0.5)))


async def _project_anchor(pipeline_id: str, db: AsyncSession) -> datetime:
    """Tier-0 activation anchor for a project's goals: when the product started
    being tracked. Uses launch date if launched, else the project's creation."""
    pipeline = await db.get(Pipeline, pipeline_id)
    if pipeline is None:
        return _now()
    return pipeline.launched_at or pipeline.created_at or _now()


async def _account_anchor(uid: str, db: AsyncSession) -> datetime:
    """Tier-0 activation anchor for account goals: the user's earliest project
    creation (a proxy for when they started), falling back to now."""
    earliest = await db.scalar(
        select(func.min(Pipeline.created_at)).where(Pipeline.user_id == uid)
    )
    return earliest or _now()


def _shape_goal(
    definition: GoalDefinition,
    tiers: list[GoalTier],
    value: int,
    achieved: dict[int, datetime],
    anchor: datetime,
    now: datetime,
) -> dict:
    """Build the UI payload for one goal.

    Encodes the "clock starts only when active" rule. Dates are derived, not
    stored: a tier's activation date = the previous tier's completion date (or the
    group's ``anchor`` for tier 0); its target date = activation + estimate_days.
    Completed and active tiers carry activation/target dates; upcoming tiers carry
    only the configured estimate (no live countdown)."""
    ordered = sorted(tiers, key=lambda t: t.tier_index)
    max_threshold = ordered[-1].threshold_value if ordered else 0
    # Achievements are a prefix (thresholds increase), so the first un-achieved
    # tier is the single active milestone; None means the whole group is complete.
    active_pos = next((i for i, t in enumerate(ordered) if t.tier_index not in achieved), None)

    def activation_of(pos: int) -> datetime | None:
        """When the tier at ``pos`` became/would become active: the anchor for
        tier 0, else the previous tier's completion date (None if not yet done)."""
        if pos == 0:
            return anchor
        return achieved.get(ordered[pos - 1].tier_index)

    tier_views: list[dict] = []
    for pos, t in enumerate(ordered):
        completed = t.tier_index in achieved
        is_active = active_pos is not None and pos == active_pos
        state = "completed" if completed else "active" if is_active else "upcoming"
        prev_threshold = ordered[pos - 1].threshold_value if pos > 0 else 0
        estimate_days = _tier_estimate_days(t.threshold_value, prev_threshold, t.estimate_days)
        # Only active/completed tiers have a live activation + target; upcoming
        # tiers expose the estimate only (soft projection is the UI's job).
        activated = activation_of(pos) if state in ("completed", "active") else None
        target = activated + timedelta(days=estimate_days) if activated is not None else None
        completed_at = achieved.get(t.tier_index)
        days_left = (target - now).days if state == "active" and target is not None else None
        tier_views.append({
            "tierIndex": t.tier_index,
            "threshold": t.threshold_value,
            "label": t.label,
            "achieved": completed,
            "state": state,
            "estimateDays": estimate_days,
            "activatedAt": activated.isoformat() if activated else None,
            "targetDate": target.isoformat() if target else None,
            "completedAt": completed_at.isoformat() if completed_at else None,
            "daysLeft": days_left,
        })

    milestones = [
        {
            "tierIndex": v["tierIndex"],
            "label": v["label"],
            "threshold": v["threshold"],
            "achievedAt": v["completedAt"],
            "activatedAt": v["activatedAt"],
            "targetDate": v["targetDate"],
        }
        for v in tier_views if v["state"] == "completed"
    ]
    milestones.sort(key=lambda m: m["achievedAt"] or "")

    active_view = tier_views[active_pos] if active_pos is not None else None
    return {
        "id": definition.id,
        "category": definition.category,
        "title": definition.title,
        "metricKey": definition.metric_key,
        "icon": definition.icon,
        "currentValue": value,
        "achievedCount": len(achieved),
        "tierCount": len(ordered),
        "maxThreshold": max_threshold,
        # Group-level state: an active milestone remains, or the whole group is done.
        "state": "active" if active_view is not None else "completed",
        "tiers": tier_views,
        # Back-compat: nextTier mirrors the active tier's identity.
        "nextTier": None if active_view is None else {
            "tierIndex": active_view["tierIndex"],
            "threshold": active_view["threshold"],
            "label": active_view["label"],
        },
        # The active milestone with its live activation/target/countdown.
        "activeTier": None if active_view is None else {
            "tierIndex": active_view["tierIndex"],
            "threshold": active_view["threshold"],
            "label": active_view["label"],
            "estimateDays": active_view["estimateDays"],
            "activatedAt": active_view["activatedAt"],
            "targetDate": active_view["targetDate"],
            "daysLeft": active_view["daysLeft"],
        },
        "activatedAt": active_view["activatedAt"] if active_view else None,
        "targetDate": active_view["targetDate"] if active_view else None,
        "milestones": milestones,
    }


def _visible_project_defs(
    defs: list[tuple[GoalDefinition, list[GoalTier]]], launched: bool
) -> list[tuple[GoalDefinition, list[GoalTier]]]:
    """Journey goals (requires_launch == False) always show; outcome goals only
    once the project has launched."""
    if launched:
        return defs
    return [(d, tiers) for d, tiers in defs if not d.requires_launch]


async def project_goals_payload(pipeline_id: str, db: AsyncSession, launched: bool = True) -> list[dict]:
    """Evaluate then shape a project's goals for the UI. Before launch only the
    pre-launch journey goals (requires_launch == False) are returned; once
    launched, the post-launch outcome goals join them."""
    await evaluate_project_goals(pipeline_id, db)
    await db.commit()
    defs = _visible_project_defs(await _definitions_with_tiers("project", db), launched)
    values = await project_metric_values(pipeline_id, db)
    achieved_rows = (await db.execute(
        select(ProjectGoalAchievement).where(ProjectGoalAchievement.project_id == pipeline_id)
    )).scalars().all()
    achieved_by_goal: dict[str, dict[int, datetime]] = {}
    for row in achieved_rows:
        achieved_by_goal.setdefault(row.goal_definition_id, {})[row.tier_index] = row.achieved_at
    anchor = await _project_anchor(pipeline_id, db)
    now = _now()
    return [
        _shape_goal(d, tiers, int(values.get(d.metric_key, 0)), achieved_by_goal.get(d.id, {}), anchor, now)
        for d, tiers in defs
    ]


async def account_goals_payload(uid: str, db: AsyncSession) -> list[dict]:
    """Evaluate then shape an account's portfolio-level goals for the UI."""
    await evaluate_account_goals(uid, db)
    await db.commit()
    defs = await _definitions_with_tiers("account", db)
    values = await account_metric_values(uid, db)
    achieved_rows = (await db.execute(
        select(AccountGoalAchievement).where(AccountGoalAchievement.user_id == uid)
    )).scalars().all()
    achieved_by_goal: dict[str, dict[int, datetime]] = {}
    for row in achieved_rows:
        achieved_by_goal.setdefault(row.goal_definition_id, {})[row.tier_index] = row.achieved_at
    anchor = await _account_anchor(uid, db)
    now = _now()
    return [
        _shape_goal(d, tiers, int(values.get(d.metric_key, 0)), achieved_by_goal.get(d.id, {}), anchor, now)
        for d, tiers in defs
    ]
