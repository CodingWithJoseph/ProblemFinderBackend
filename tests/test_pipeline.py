from conftest import FakeResult, make_issue, make_pipeline, make_team


async def test_pipeline_list_includes_project_team_and_issue_counts(client, fake_db, auth_headers):
    team = make_team(name="Validation Team")
    card = make_pipeline(team_id=team.id)
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
    assert row["openIssueCount"] == 2
    assert row["openKillCriteriaCount"] == 1


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
