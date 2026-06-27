"""Tests for the /dashboard route.

Focus: GET /dashboard/summary returns the correct aggregate counts across the
user's pipelines, problems, and tasks.
"""

from datetime import datetime, timedelta, timezone

from conftest import FakeResult, make_pipeline
from app.services.activity import should_record_user_action


async def test_dashboard_summary_counts(client, fake_db, auth_headers):
    active = make_pipeline(
        name="Active cluster",
        post_ids=["p1", "p2"],
        launched_at=None,
        updated_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        stage="validating",
    )
    launched = make_pipeline(
        name="Launched cluster",
        launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
    )

    fake_db.stub(
        execute=[
            FakeResult(rows=[active, launched]),                 # pipelines
            FakeResult(rows=[(active.id, 2), (launched.id, 1)]),  # per-pipeline problem counts
            FakeResult(rows=[(active.id, 3, 1)]),                 # per-pipeline task (total, open)
        ]
    )

    resp = await client.get("/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["clustersTracked"] == 2
    assert body["activeCount"] == 1
    assert body["launchedCount"] == 1
    assert body["problemsDefined"] == 3       # 2 + 1
    assert body["totalTasks"] == 3
    assert body["openTasks"] == 1

    # Only active pipelines are listed in the progress column.
    assert len(body["pipelines"]) == 1
    row = body["pipelines"][0]
    assert row["id"] == active.id
    assert row["name"] == "Active cluster"
    assert row["postCount"] == 2
    assert row["problemCount"] == 2
    assert row["taskCount"] == 3
    assert row["openTaskCount"] == 1

    # Recent activity is sorted by updatedAt desc (active is more recent).
    assert [a["id"] for a in body["recentActivity"]] == [active.id, launched.id]


async def test_dashboard_summary_empty(client, fake_db, auth_headers):
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),   # no pipelines
            FakeResult(rows=[]),   # no problem counts
            FakeResult(rows=[]),   # no task counts
        ]
    )

    resp = await client.get("/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["clustersTracked"] == 0
    assert body["activeCount"] == 0
    assert body["problemsDefined"] == 0
    assert body["pipelines"] == []
    assert body["recentActivity"] == []


async def test_dashboard_activity_is_scoped_to_user_rollups(client, fake_db, auth_headers):
    now = datetime.now(timezone.utc)
    fake_db.stub(execute=[FakeResult(rows=[
        (now.date() - timedelta(days=1), 1, 3, now - timedelta(days=1)),
        (now.date(), 2, 4, now),
    ])])

    resp = await client.get("/dashboard/activity?weeks=26", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["days"] == [
        {"date": (now.date() - timedelta(days=1)).isoformat(), "count": 4},
        {"date": now.date().isoformat(), "count": 6},
    ]
    assert body["windowActions"] == 7
    assert body["windowLogins"] == 3
    assert body["activeDays"] == 2
    assert body["trend"]["current7d"] == 10


async def test_dashboard_activity_records_login(client, fake_db, auth_headers):
    resp = await client.post(
        "/dashboard/activity",
        headers=auth_headers,
        json={"kind": "login"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    assert fake_db.commit_count == 1


def test_only_successful_user_work_requests_count_as_actions():
    assert should_record_user_action("PATCH", "/tasks/task-1", 200)
    assert should_record_user_action("POST", "/pipeline", 201)
    assert not should_record_user_action("GET", "/tasks", 200)
    assert not should_record_user_action("POST", "/dashboard/activity", 200)
    assert not should_record_user_action("POST", "/public/portfolio/events", 202)
    assert not should_record_user_action("DELETE", "/issues/issue-1", 404)
