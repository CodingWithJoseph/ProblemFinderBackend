from datetime import datetime, timezone

from conftest import FakeResult, make_issue, make_pipeline, make_team
from app.models import MonitorUsageSource


async def test_pipeline_list_includes_project_team_and_issue_counts(client, fake_db, auth_headers):
    team = make_team(name="Validation Team")
    card = make_pipeline(team_id=team.id, icon_url="https://example.com/product.png")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),  # active cards
        FakeResult(rows=[team]),  # assigned team summaries
        FakeResult(rows=[
            (card.id, "issue", 2),
            (card.id, "kill_criteria", 1),
        ]),
    ])

    resp = await client.get("/pipeline", headers=auth_headers)

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["teamId"] == team.id
    assert row["team"]["name"] == "Validation Team"
    assert row["iconUrl"] == "https://example.com/product.png"
    assert row["openIssueCount"] == 2
    assert row["openKillCriteriaCount"] == 1


async def test_portfolio_list_serializes_product_icon(client, fake_db, auth_headers):
    card = make_pipeline(
        launched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        icon_url="https://example.com/launched-product.png",
    )
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[]),
    ])

    resp = await client.get("/portfolio", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"][0]["iconUrl"] == "https://example.com/launched-product.png"


async def test_pipeline_team_change_updates_issues_and_clears_assignees(client, fake_db, auth_headers):
    old_team = make_team(name="Old Team")
    new_team = make_team(name="New Team")
    card = make_pipeline(team_id=old_team.id)
    issue = make_issue(pipeline_id=card.id, team_id=old_team.id, assignee_id="member-1")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),      # card lookup
        FakeResult(rows=[new_team]),  # new team visibility
        FakeResult(rows=[issue]),     # linked issues sync
        FakeResult(rows=[new_team]),  # response team context
        FakeResult(rows=[]),          # issue counts
    ])

    resp = await client.patch(f"/pipeline/{card.id}", json={"team_id": new_team.id}, headers=auth_headers)

    assert resp.status_code == 200
    assert card.team_id == new_team.id
    assert issue.team_id == new_team.id
    assert issue.assignee_id is None


async def test_display_name_falls_back_to_cluster_name(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42", project_name=None)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),  # single-card lookup
        FakeResult(rows=[]),      # issue counts
    ])

    resp = await client.get(f"/pipeline/{card.id}", headers=auth_headers)

    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["projectName"] is None
    assert row["displayName"] == "Cluster 42"


async def test_patch_sets_project_name_and_timeline(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),  # card lookup
        FakeResult(rows=[]),      # issue counts
    ])

    resp = await client.patch(
        f"/pipeline/{card.id}",
        json={"project_name": "Invoice Ninja", "timeline_days": 30},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["projectName"] == "Invoice Ninja"
    assert row["displayName"] == "Invoice Ninja"
    assert row["timelineDays"] == 30
    assert row["timelineStart"] is not None
    assert row["timelineTargetLaunch"] is not None
    # target launch is start + 30 days
    assert card.timeline_target_launch - card.timeline_start == __import__("datetime").timedelta(days=30)


async def test_patch_sets_product_icon(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[]),
    ])

    icon_url = "data:image/png;base64,aWNvbg=="
    resp = await client.patch(
        f"/pipeline/{card.id}",
        json={"icon_url": icon_url},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["iconUrl"] == icon_url
    assert card.icon_url == icon_url


async def test_patch_rejects_unsupported_product_icon(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42")
    fake_db.stub(execute=[FakeResult(rows=[card])])

    resp = await client.patch(
        f"/pipeline/{card.id}",
        json={"icon_url": "javascript:alert(1)"},
        headers=auth_headers,
    )

    assert resp.status_code == 400


async def test_patch_skip_sets_project_name_only(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),  # card lookup
        FakeResult(rows=[]),      # issue counts
    ])

    resp = await client.patch(
        f"/pipeline/{card.id}",
        json={"project_name": "Just A Name"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["projectName"] == "Just A Name"
    assert row["timelineDays"] is None
    assert card.timeline_start is None


async def test_patch_rejects_invalid_timeline_days(client, fake_db, auth_headers):
    card = make_pipeline()
    fake_db.stub(execute=[FakeResult(rows=[card])])

    resp = await client.patch(
        f"/pipeline/{card.id}",
        json={"timeline_days": 45},
        headers=auth_headers,
    )

    assert resp.status_code == 400


async def test_launch_does_not_create_or_connect_monitoring(client, fake_db, auth_headers):
    card = make_pipeline(name="PC Boot Failures and Hardware Damage")
    fake_db.stub(execute=[FakeResult(rows=[card])])

    resp = await client.post(
        f"/pipeline/{card.id}/launch",
        json={"product_name": card.name},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["launchedAt"] is not None
    assert not any(isinstance(obj, MonitorUsageSource) for obj in fake_db.added)


async def test_patch_stage_records_a_stage_event(client, fake_db, auth_headers):
    from app.models import PipelineStageEvent
    card = make_pipeline(stage="watching")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),  # card lookup
        FakeResult(rows=[]),      # issue counts (serialization)
    ])

    resp = await client.patch(f"/pipeline/{card.id}", json={"stage": "exploring"}, headers=auth_headers)

    assert resp.status_code == 200
    assert card.stage == "exploring"
    events = [o for o in fake_db.added if isinstance(o, PipelineStageEvent)]
    assert len(events) == 1
    assert events[0].stage == "exploring"
    assert events[0].pipeline_id == str(card.id)


async def test_patch_unchanged_stage_records_no_event(client, fake_db, auth_headers):
    from app.models import PipelineStageEvent
    card = make_pipeline(stage="exploring")
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[])])

    resp = await client.patch(f"/pipeline/{card.id}", json={"stage": "exploring"}, headers=auth_headers)

    assert resp.status_code == 200
    assert not [o for o in fake_db.added if isinstance(o, PipelineStageEvent)]


async def test_get_card_includes_ordered_stage_events(client, fake_db, auth_headers):
    from app.models import PipelineStageEvent
    card = make_pipeline()
    ev1 = PipelineStageEvent(pipeline_id=card.id, stage="watching", entered_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
    ev2 = PipelineStageEvent(pipeline_id=card.id, stage="exploring", entered_at=datetime(2026, 5, 10, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[card]),        # card lookup
        FakeResult(rows=[]),            # issue counts
        FakeResult(rows=[ev1, ev2]),    # stage events
    ])

    resp = await client.get(f"/pipeline/{card.id}", headers=auth_headers)

    assert resp.status_code == 200
    stage_events = resp.json()["data"]["stageEvents"]
    assert [e["stage"] for e in stage_events] == ["watching", "exploring"]
    assert stage_events[0]["enteredAt"].startswith("2026-05-01")
