from conftest import (
    FakeResult,
    TEST_UID,
    make_issue,
    make_issue_comment,
    make_pipeline,
    make_team,
    make_team_member,
)
from app.models import Issue, IssueComment


async def test_create_issue_requires_project(client, fake_db, auth_headers):
    resp = await client.post(
        "/issues",
        json={"title": "Analyze signals", "summary": "Look at source evidence."},
        headers=auth_headers,
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Issue project is required"
    assert fake_db.added == []


async def test_create_kill_criteria_issue(client, fake_db, auth_headers):
    pipeline = make_pipeline()
    fake_db.stub(execute=[
        FakeResult(rows=[pipeline]),  # _require_pipeline
        FakeResult(rows=[pipeline]),  # response context
    ])

    resp = await client.post(
        "/issues",
        json={"title": "No willingness to pay", "pipeline_id": pipeline.id, "issue_type": "kill_criteria"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["issueType"] == "kill_criteria"
    assert body["project"]["name"] == pipeline.name
    assert any(isinstance(obj, Issue) and obj.issue_type == "kill_criteria" for obj in fake_db.added)


async def test_create_pipeline_issue_inherits_project_team(client, fake_db, auth_headers):
    team = make_team()
    pipeline = make_pipeline(team_id=team.id)
    fake_db.stub(execute=[
        FakeResult(rows=[pipeline]),  # _require_pipeline
        FakeResult(rows=[team]),      # _require_team
        FakeResult(rows=[pipeline]),  # response project context
        FakeResult(rows=[team]),      # response team context
    ])

    resp = await client.post(
        "/issues",
        json={"title": "Validate pricing", "pipeline_id": pipeline.id},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["pipelineId"] == pipeline.id
    assert body["teamId"] == team.id
    assert body["project"]["name"] == pipeline.name
    assert body["team"]["name"] == team.name
    assert any(isinstance(obj, Issue) and obj.team_id == team.id for obj in fake_db.added)


async def test_create_issue_accepts_assignee_from_project_team(client, fake_db, auth_headers):
    team = make_team()
    member = make_team_member(team_id=team.id, display_name="Alex Builder")
    pipeline = make_pipeline(team_id=team.id)
    fake_db.stub(execute=[
        FakeResult(rows=[pipeline]),  # _require_pipeline
        FakeResult(rows=[team]),      # _require_team
        FakeResult(rows=[member]),    # _require_assignee
        FakeResult(rows=[pipeline]),  # response project context
        FakeResult(rows=[team]),      # response team context
        FakeResult(rows=[member]),    # response assignee context
    ])

    resp = await client.post(
        "/issues",
        json={"title": "Validate pricing", "pipeline_id": pipeline.id, "assignee_id": member.id},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["assigneeId"] == member.id
    assert body["assignee"]["displayName"] == "Alex Builder"
    assert any(isinstance(obj, Issue) and obj.assignee_id == member.id for obj in fake_db.added)


async def test_create_issue_rejects_assignee_outside_project_team(client, fake_db, auth_headers):
    team = make_team()
    pipeline = make_pipeline(team_id=team.id)
    fake_db.stub(execute=[
        FakeResult(rows=[pipeline]),  # _require_pipeline
        FakeResult(rows=[team]),      # _require_team
        FakeResult(rows=[]),          # _require_assignee
    ])

    resp = await client.post(
        "/issues",
        json={"title": "Validate pricing", "pipeline_id": pipeline.id, "assignee_id": "wrong-member"},
        headers=auth_headers,
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Assignee must belong to the issue team"
    assert fake_db.added == []


async def test_list_issues(client, fake_db, auth_headers):
    issue = make_issue()
    fake_db.stub(execute=[
        FakeResult(rows=[issue]),
        FakeResult(rows=[(issue.id, 2)]),
        FakeResult(rows=[(issue.id, 1)]),
    ])

    resp = await client.get("/issues", headers=auth_headers)

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["title"] == "Analyze signals"
    assert row["commentCount"] == 2
    assert row["subIssueCount"] == 1


async def test_get_issue_detail(client, fake_db, auth_headers):
    team = make_team()
    pipeline = make_pipeline(team_id=team.id)
    issue = make_issue(pipeline_id=pipeline.id, team_id=team.id)
    comment = make_issue_comment(issue_id=issue.id)
    child = make_issue(parent_issue_id=issue.id, pipeline_id=pipeline.id, team_id=team.id, title="Read posts")
    fake_db.stub(execute=[
        FakeResult(rows=[issue]),
        FakeResult(rows=[comment]),
        FakeResult(rows=[child]),
        FakeResult(rows=[(issue.id, 1)]),
        FakeResult(rows=[(issue.id, 1)]),
        FakeResult(rows=[pipeline]),
        FakeResult(rows=[team]),
    ])

    resp = await client.get(f"/issues/{issue.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["project"]["name"] == pipeline.name
    assert body["team"]["name"] == team.name
    assert body["comments"][0]["authorDisplayName"] == "You"
    assert body["comments"][0]["body"] == comment.body
    assert body["subIssues"][0]["title"] == "Read posts"


async def test_update_issue_title_summary_status(client, fake_db, auth_headers):
    pipeline = make_pipeline()
    issue = make_issue(pipeline_id=pipeline.id)
    fake_db.stub(execute=[
        FakeResult(rows=[issue]),
        FakeResult(rows=[pipeline]),
    ])

    resp = await client.patch(
        f"/issues/{issue.id}",
        json={"title": "Validate problems", "summary": "Check breakdown.", "status": "done"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "done"
    assert issue.title == "Validate problems"
    assert issue.closed_at is not None
    assert fake_db.commit_count == 1


async def test_update_issue_assignee(client, fake_db, auth_headers):
    team = make_team()
    pipeline = make_pipeline(team_id=team.id)
    member = make_team_member(team_id=team.id, display_name="Jordan")
    issue = make_issue(pipeline_id=pipeline.id, team_id=team.id)
    fake_db.stub(execute=[
        FakeResult(rows=[issue]),
        FakeResult(rows=[member]),
        FakeResult(rows=[pipeline]),
        FakeResult(rows=[team]),
        FakeResult(rows=[member]),
    ])

    resp = await client.patch(
        f"/issues/{issue.id}",
        json={"assignee_id": member.id},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert issue.assignee_id == member.id
    assert resp.json()["data"]["assignee"]["displayName"] == "Jordan"


async def test_create_and_list_comments(client, fake_db, auth_headers):
    issue = make_issue()
    comment = make_issue_comment(issue_id=issue.id)
    fake_db.stub(execute=[FakeResult(rows=[issue]), FakeResult(rows=[issue]), FakeResult(rows=[comment])])

    created = await client.post(
        f"/issues/{issue.id}/comments",
        json={"body": "This signal looks promising."},
        headers=auth_headers,
    )
    listed = await client.get(f"/issues/{issue.id}/comments", headers=auth_headers)

    assert created.status_code == 200
    assert any(isinstance(obj, IssueComment) and obj.body == "This signal looks promising." for obj in fake_db.added)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["body"] == comment.body


async def test_create_sub_issue_and_list_sub_issues(client, fake_db, auth_headers):
    pipeline = make_pipeline()
    parent = make_issue(pipeline_id=pipeline.id)
    child = make_issue(parent_issue_id=parent.id, pipeline_id=pipeline.id, title="Review source posts")
    fake_db.stub(execute=[
        FakeResult(rows=[parent]),  # create sub-issue parent access
        FakeResult(rows=[pipeline]),  # inherited pipeline access
        FakeResult(rows=[pipeline]),  # create response context
        FakeResult(rows=[child]),   # list sub-issues
        FakeResult(rows=[]),        # comment counts
        FakeResult(rows=[]),        # sub-issue counts
        FakeResult(rows=[pipeline]), # list response context
    ])

    created = await client.post(
        f"/issues/{parent.id}/subissues",
        json={"title": "Review source posts"},
        headers=auth_headers,
    )
    listed = await client.get(f"/issues?parent_issue_id={parent.id}", headers=auth_headers)

    assert created.status_code == 200
    assert created.json()["data"]["parentIssueId"] == parent.id
    assert listed.status_code == 200
    assert listed.json()["data"][0]["title"] == "Review source posts"


async def test_default_issues_created_idempotently_for_pipeline(client, fake_db, auth_headers):
    pipeline = make_pipeline()
    existing = make_issue(pipeline_id=pipeline.id, source="analyze_signals")
    fake_db.stub(execute=[
        FakeResult(rows=[pipeline]),  # _ensure_default_issues_for_pipeline ownership
        FakeResult(rows=[("analyze_signals",)]),  # existing defaults
        FakeResult(rows=[existing]),  # list issues
        FakeResult(rows=[]),          # comment counts
        FakeResult(rows=[]),          # sub-issue counts
        FakeResult(rows=[pipeline]),  # response context
        FakeResult(rows=[pipeline]),  # second call ownership
        FakeResult(rows=[
            ("analyze_signals",),
            ("validate_breakdown_problems",),
            ("create_tasks",),
        ]),
        FakeResult(rows=[existing]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
        FakeResult(rows=[pipeline]),
    ])

    first = await client.get(f"/issues?pipeline_id={pipeline.id}", headers=auth_headers)
    first_added = [obj for obj in fake_db.added if isinstance(obj, Issue)]
    second = await client.get(f"/issues?pipeline_id={pipeline.id}", headers=auth_headers)
    second_added = [obj for obj in fake_db.added if isinstance(obj, Issue)]

    assert first.status_code == 200
    assert second.status_code == 200
    assert {obj.source for obj in first_added} == {"validate_breakdown_problems", "create_tasks"}
    assert len(second_added) == len(first_added)


async def test_team_member_can_access_issue_through_assigned_project(client, fake_db, auth_headers):
    team = make_team(owner_user_id="owner-uid")
    member = make_team_member(team_id=team.id, user_id=TEST_UID)
    pipeline = make_pipeline(user_id="owner-uid", team_id=team.id)
    issue = make_issue(user_id="owner-uid", pipeline_id=pipeline.id, team_id=team.id)
    fake_db.stub(execute=[
        FakeResult(rows=[issue]),    # issue lookup
        FakeResult(rows=[pipeline]), # pipeline visibility
        FakeResult(rows=[team]),     # team visibility lookup
        FakeResult(rows=[member]),   # team membership lookup
        FakeResult(rows=[]),         # comments
        FakeResult(rows=[]),         # sub-issues
        FakeResult(rows=[]),         # comment counts
        FakeResult(rows=[]),         # sub-issue counts
        FakeResult(rows=[pipeline]), # response project context
        FakeResult(rows=[team]),     # response team context
    ])

    resp = await client.get(f"/issues/{issue.id}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["project"]["id"] == pipeline.id
    assert resp.json()["data"]["team"]["id"] == team.id


async def test_unauthorized_access_blocked(client, fake_db, auth_headers):
    issue = make_issue(user_id="someone-else")
    fake_db.stub(execute=[FakeResult(rows=[issue])])

    resp = await client.get(f"/issues/{issue.id}", headers=auth_headers)

    assert resp.status_code == 404
