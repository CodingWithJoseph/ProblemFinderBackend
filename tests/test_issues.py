from conftest import (
    FakeResult,
    TEST_UID,
    make_issue,
    make_issue_comment,
    make_pipeline,
)
from app.models import Issue, IssueComment


async def test_create_issue(client, fake_db, auth_headers):
    resp = await client.post(
        "/issues",
        json={"title": "Analyze signals", "summary": "Look at source evidence."},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["title"] == "Analyze signals"
    assert body["status"] == "open"
    assert any(isinstance(obj, Issue) and obj.title == "Analyze signals" for obj in fake_db.added)


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
    issue = make_issue()
    comment = make_issue_comment(issue_id=issue.id)
    child = make_issue(parent_issue_id=issue.id, title="Read posts")
    fake_db.stub(execute=[
        FakeResult(rows=[issue]),
        FakeResult(rows=[comment]),
        FakeResult(rows=[child]),
        FakeResult(rows=[(issue.id, 1)]),
        FakeResult(rows=[(issue.id, 1)]),
    ])

    resp = await client.get(f"/issues/{issue.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["comments"][0]["body"] == comment.body
    assert body["subIssues"][0]["title"] == "Read posts"


async def test_update_issue_title_summary_status(client, fake_db, auth_headers):
    issue = make_issue()
    fake_db.stub(execute=[FakeResult(rows=[issue])])

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
    parent = make_issue()
    child = make_issue(parent_issue_id=parent.id, title="Review source posts")
    fake_db.stub(execute=[
        FakeResult(rows=[parent]),  # create sub-issue parent access
        FakeResult(rows=[child]),   # list sub-issues
        FakeResult(rows=[]),        # comment counts
        FakeResult(rows=[]),        # sub-issue counts
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
        FakeResult(rows=[pipeline]),  # second call ownership
        FakeResult(rows=[
            ("analyze_signals",),
            ("validate_breakdown_problems",),
            ("create_tasks",),
        ]),
        FakeResult(rows=[existing]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
    ])

    first = await client.get(f"/issues?pipeline_id={pipeline.id}", headers=auth_headers)
    first_added = [obj for obj in fake_db.added if isinstance(obj, Issue)]
    second = await client.get(f"/issues?pipeline_id={pipeline.id}", headers=auth_headers)
    second_added = [obj for obj in fake_db.added if isinstance(obj, Issue)]

    assert first.status_code == 200
    assert second.status_code == 200
    assert {obj.source for obj in first_added} == {"validate_breakdown_problems", "create_tasks"}
    assert len(second_added) == len(first_added)


async def test_unauthorized_access_blocked(client, fake_db, auth_headers):
    issue = make_issue(user_id="someone-else")
    fake_db.stub(execute=[FakeResult(rows=[issue])])

    resp = await client.get(f"/issues/{issue.id}", headers=auth_headers)

    assert resp.status_code == 404
