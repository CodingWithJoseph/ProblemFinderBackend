from datetime import datetime, timezone

from conftest import FakeResult
from app.models import GoalDefinition, GoalTier, MonitorRevenueSource
from app.services import goals as goals_service


def _tier(goal_id, index, threshold, label):
    return GoalTier(id=f"{goal_id}-{index}", goal_definition_id=goal_id, tier_index=index, threshold_value=threshold, label=label)


def test_newly_crossed_indices_returns_unrecorded_met_tiers():
    tiers = [_tier("g", 0, 5, "5"), _tier("g", 1, 10, "10"), _tier("g", 2, 50, "50")]
    # value 12 meets tiers 0 and 1; tier 0 already recorded → only 1 is new.
    assert goals_service._newly_crossed_indices(12, tiers, already={0}) == [1]
    # value below the first threshold → nothing.
    assert goals_service._newly_crossed_indices(3, tiers, already=set()) == []
    # a jump can cross several at once.
    assert goals_service._newly_crossed_indices(60, tiers, already=set()) == [0, 1, 2]


def test_selected_mrr_cents_respects_engine():
    source = MonitorRevenueSource(id="s", pipeline_id="p", user_id="u", current_mrr_cents=1500, invoice_mrr_cents=1800)
    assert goals_service._selected_mrr_cents(source, "subscription") == 1500
    assert goals_service._selected_mrr_cents(source, "invoice") == 1800
    assert goals_service._selected_mrr_cents(None, "invoice") == 0


def test_shape_goal_reports_next_tier_and_milestones():
    definition = GoalDefinition(id="proj_signups", scope="project", category="growth", title="Signups", metric_key="signups", icon="user-plus", sort_order=10)
    tiers = [_tier("proj_signups", 0, 5, "5"), _tier("proj_signups", 1, 10, "10"), _tier("proj_signups", 2, 50, "50")]
    achieved = {
        0: datetime(2026, 6, 1, tzinfo=timezone.utc),
        1: datetime(2026, 6, 10, tzinfo=timezone.utc),
    }

    anchor = datetime(2026, 5, 1, tzinfo=timezone.utc)
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    shaped = goals_service._shape_goal(definition, tiers, value=12, achieved=achieved, anchor=anchor, now=now)

    assert shaped["currentValue"] == 12
    assert shaped["achievedCount"] == 2
    assert shaped["tierCount"] == 3
    assert shaped["icon"] == "user-plus"
    # Next un-achieved tier is 50 (value 12 < 50 but >= 10).
    assert shaped["nextTier"]["threshold"] == 50
    assert [t["achieved"] for t in shaped["tiers"]] == [True, True, False]
    # Milestone log is chronological and only the achieved tiers.
    assert [m["label"] for m in shaped["milestones"]] == ["5", "10"]
    # Active tier 50 activates at the previous tier's completion (Jun 10).
    assert shaped["activeTier"]["threshold"] == 50
    assert shaped["activatedAt"].startswith("2026-06-10")
    assert shaped["state"] == "active"


def test_shape_goal_next_tier_none_when_complete():
    definition = GoalDefinition(id="acct_team", scope="account", category="team", title="Team", metric_key="teammates_invited", icon="users", sort_order=40)
    tiers = [_tier("acct_team", 0, 1, "1"), _tier("acct_team", 1, 5, "5")]
    anchor = datetime(2026, 6, 1, tzinfo=timezone.utc)
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    shaped = goals_service._shape_goal(definition, tiers, value=9, achieved={0: datetime(2026, 6, 1, tzinfo=timezone.utc), 1: datetime(2026, 6, 2, tzinfo=timezone.utc)}, anchor=anchor, now=now)
    assert shaped["nextTier"] is None
    assert shaped["activeTier"] is None
    # A fully-cleared group reports as complete (the UI's Archived section).
    assert shaped["state"] == "completed"


def test_shape_goal_clock_starts_on_activation():
    """The core rule: a tier's clock starts only when it becomes active, anchored
    to the *actual* previous completion date — not a pre-planned schedule."""
    definition = GoalDefinition(id="proj_signups", scope="project", category="growth", title="Signups", metric_key="signups", icon="user-plus", sort_order=10)
    tiers = [
        GoalTier(id="t0", goal_definition_id="proj_signups", tier_index=0, threshold_value=5, label="5", estimate_days=21),
        GoalTier(id="t1", goal_definition_id="proj_signups", tier_index=1, threshold_value=10, label="10", estimate_days=30),
        GoalTier(id="t2", goal_definition_id="proj_signups", tier_index=2, threshold_value=100, label="100", estimate_days=60),
    ]
    anchor = datetime(2026, 6, 1, tzinfo=timezone.utc)          # tier 0 activation
    achieved = {0: datetime(2026, 8, 5, tzinfo=timezone.utc)}   # completed late (Aug 5, not the Jun 22 plan)
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    shaped = goals_service._shape_goal(definition, tiers, value=6, achieved=achieved, anchor=anchor, now=now)

    t0, t1, t2 = shaped["tiers"]
    # Completed tier 0: activated at the anchor, planned target anchor+21d, done Aug 5.
    assert t0["state"] == "completed"
    assert t0["activatedAt"].startswith("2026-06-01")
    assert t0["targetDate"].startswith("2026-06-22")
    assert t0["completedAt"].startswith("2026-08-05")
    # Active tier 1: clock starts at the real previous completion (Aug 5) + 30d.
    assert t1["state"] == "active"
    assert t1["activatedAt"].startswith("2026-08-05")
    assert t1["targetDate"].startswith("2026-09-04")
    assert t1["daysLeft"] == 15
    # Upcoming tier 2: estimate only — no activation, target, or countdown yet.
    assert t2["state"] == "upcoming"
    assert t2["activatedAt"] is None
    assert t2["targetDate"] is None
    assert t2["daysLeft"] is None
    assert t2["estimateDays"] == 60


async def test_account_metric_values_reads_rollups(fake_db):
    # scalar() calls, in order: products_launched, problems_created, tasks_created, teammates_invited
    fake_db.stub(scalar=[3, 42, 7, 2])
    values = await goals_service.account_metric_values("uid-1", fake_db)
    assert values == {
        "products_launched": 3,
        "problems_created": 42,
        "tasks_created": 7,
        "teammates_invited": 2,
    }


async def test_project_metric_values_includes_journey_metrics(fake_db):
    # scalar() order: revenue_source, signups, traffic, issues_created,
    # problems_defined, features_defined, features_built, then _project_setup_steps
    # (usage_source, usage_flowing, revenue_source, errors_flowing).
    fake_db.stub(scalar=[None, 3, 100, 2, 7, 12, 4, None, None, None, None])
    values = await goals_service.project_metric_values("pipe-1", fake_db)
    assert values["problems_defined"] == 7
    assert values["features_defined"] == 12
    assert values["features_built"] == 4
    # Existing outcome metrics still present.
    assert values["signups"] == 3
    assert values["setup_steps"] == 0


def _def(id, requires_launch):
    return GoalDefinition(id=id, scope="project", category="c", title=id, metric_key=id, icon="x", sort_order=0, requires_launch=requires_launch)


def test_visible_project_defs_gates_outcome_goals_pre_launch():
    defs = [(_def("proj_problems", False), []), (_def("proj_signups", True), [])]
    # Pre-launch: only the journey goal (requires_launch False) is visible.
    assert [d.id for d, _ in goals_service._visible_project_defs(defs, launched=False)] == ["proj_problems"]
    # Launched: both show.
    assert [d.id for d, _ in goals_service._visible_project_defs(defs, launched=True)] == ["proj_problems", "proj_signups"]


async def test_evaluate_account_goals_records_crossed_tiers(fake_db):
    launches = GoalDefinition(id="acct_launches", scope="account", category="portfolio", title="Launch products", metric_key="products_launched", icon="rocket", sort_order=10)
    team = GoalDefinition(id="acct_team", scope="account", category="team", title="Team", metric_key="teammates_invited", icon="users", sort_order=40)
    tiers = [
        _tier("acct_launches", 0, 1, "1"), _tier("acct_launches", 1, 5, "5"),
        _tier("acct_launches", 2, 10, "10"), _tier("acct_launches", 3, 25, "25"),
        _tier("acct_team", 0, 1, "1"), _tier("acct_team", 1, 5, "5"),
    ]
    # execute() order: definitions, tiers, existing achievements (empty). Inserts
    # fall through to a default empty result.
    fake_db.stub(
        execute=[FakeResult(rows=[launches, team]), FakeResult(rows=tiers), FakeResult(rows=[])],
        # account_metric_values scalars: products_launched=6, problems=0, tasks=0, teammates=0
        scalar=[6, 0, 0, 0],
    )

    recorded = await goals_service.evaluate_account_goals("uid-1", fake_db)

    # products_launched=6 crosses tiers 0 (1) and 1 (5); team=0 crosses none.
    assert recorded == 2
