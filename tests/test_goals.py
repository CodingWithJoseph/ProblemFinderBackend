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

    shaped = goals_service._shape_goal(definition, tiers, value=12, achieved=achieved)

    assert shaped["currentValue"] == 12
    assert shaped["achievedCount"] == 2
    assert shaped["tierCount"] == 3
    assert shaped["icon"] == "user-plus"
    # Next un-achieved tier is 50 (value 12 < 50 but >= 10).
    assert shaped["nextTier"]["threshold"] == 50
    assert [t["achieved"] for t in shaped["tiers"]] == [True, True, False]
    # Milestone log is chronological and only the achieved tiers.
    assert [m["label"] for m in shaped["milestones"]] == ["5", "10"]


def test_shape_goal_next_tier_none_when_complete():
    definition = GoalDefinition(id="acct_team", scope="account", category="team", title="Team", metric_key="teammates_invited", icon="users", sort_order=40)
    tiers = [_tier("acct_team", 0, 1, "1"), _tier("acct_team", 1, 5, "5")]
    shaped = goals_service._shape_goal(definition, tiers, value=9, achieved={0: datetime(2026, 6, 1, tzinfo=timezone.utc), 1: datetime(2026, 6, 2, tzinfo=timezone.utc)})
    assert shaped["nextTier"] is None


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
