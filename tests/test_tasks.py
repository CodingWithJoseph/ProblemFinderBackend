from datetime import date, datetime, timedelta, timezone

from conftest import FakeResult, make_pipeline, make_task


async def test_create_task_with_due_date(client, fake_db, auth_headers):
    pipeline = make_pipeline()
    fake_db.stub(execute=[
        FakeResult(rows=[pipeline.id]),  # pipeline ownership check
    ])

    resp = await client.post(
        "/tasks",
        json={"pipeline_id": pipeline.id, "title": "Ship landing page", "due_date": "2026-07-01"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["title"] == "Ship landing page"
    assert row["dueDate"] == "2026-07-01"
    # The created task object got the parsed date.
    assert fake_db.added[0].due_date == date(2026, 7, 1)


async def test_patch_task_sets_and_clears_due_date(client, fake_db, auth_headers):
    task = make_task(due_date=date(2026, 7, 1))
    fake_db.stub(execute=[FakeResult(rows=[task])])

    resp = await client.patch(f"/tasks/{task.id}", json={"due_date": None}, headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["dueDate"] is None
    assert task.due_date is None


async def test_deadline_summary_counts_overdue_and_due_soon(client, fake_db, auth_headers):
    today = datetime.now(timezone.utc).date()
    rows = [
        (today - timedelta(days=2), "todo"),         # overdue
        (today + timedelta(days=1), "in_progress"),  # due soon
        (today + timedelta(days=30), "todo"),         # later, not counted in soon/overdue
        (today - timedelta(days=5), "done"),          # done -> ignored
    ]
    fake_db.stub(execute=[FakeResult(rows=rows)])

    resp = await client.get("/tasks/deadline-summary?pipeline_id=p1", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["overdue"] == 1
    assert body["dueSoon"] == 1
    # soonest open due date is the overdue one
    assert body["nextDueDate"] == (today - timedelta(days=2)).isoformat()
