"""Tests for the /dashboard route.

Focus: GET /dashboard/summary returns the correct aggregate counts across the
user's pipelines, problems, and tasks.
"""

from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline


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
