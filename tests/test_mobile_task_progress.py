import pytest

from conftest import FakeResult, make_pipeline
from app.models import SolutionTaskState
from test_solution_workspace import _ready_solution


def _approved_solution(card):
    case, version = _ready_solution(card)
    case.approval_status = "approved"
    case.approved_version_id = version.id
    return case, version


def _path(card, task_id="task-1"):
    return f"/pipeline/{card.id}/solution/case/tasks/{task_id}"


async def test_app_task_progress_is_persisted_without_creating_a_build_version(client, fake_db, auth_headers):
    card = make_pipeline()
    case, version = _approved_solution(card)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[case]), FakeResult(rows=[version]), FakeResult(rows=[])])
    response = await client.patch(_path(card), headers=auth_headers, json={"expectedVersion": 1, "status": "in_progress"})
    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    assert response.json()["taskId"] == "task-1"
    assert len(fake_db.added) == 1
    saved = fake_db.added[0]
    assert isinstance(saved, SolutionTaskState)
    assert saved.status == "in_progress"
    assert saved.updated_by == "app"
    assert case.current_version_id == version.id
    assert case.approval_status == "approved"


async def test_app_task_progress_rejects_stale_build_version(client, fake_db, auth_headers):
    card = make_pipeline()
    case, version = _approved_solution(card)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[case]), FakeResult(rows=[version])])
    response = await client.patch(_path(card), headers=auth_headers, json={"expectedVersion": 2, "status": "todo"})
    assert response.status_code == 409
    assert fake_db.commit_count == 0


@pytest.mark.parametrize("payload", [
    {"expectedVersion": 1, "status": "blocked"},
    {"expectedVersion": 1, "status": "done"},
    {"expectedVersion": 1, "status": "done", "verification": {"summary": "Checked", "checks": [{"command": "test", "outcome": "failed"}]}},
])
async def test_app_task_progress_requires_blocker_or_passing_verification(payload, client, fake_db, auth_headers):
    response = await client.patch("/pipeline/project-1/solution/case/tasks/task-1", headers=auth_headers, json=payload)
    assert response.status_code == 422
    assert fake_db.commit_count == 0


async def test_app_task_progress_requires_owned_project(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    response = await client.patch("/pipeline/other-project/solution/case/tasks/task-1", headers=auth_headers,
                                  json={"expectedVersion": 1, "status": "todo"})
    assert response.status_code == 404
    assert fake_db.commit_count == 0


async def test_app_task_progress_rejects_unknown_task(client, fake_db, auth_headers):
    card = make_pipeline()
    case, version = _approved_solution(card)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[case]), FakeResult(rows=[version])])
    response = await client.patch(_path(card, "other-task"), headers=auth_headers, json={"expectedVersion": 1, "status": "todo"})
    assert response.status_code == 404
    assert fake_db.commit_count == 0


async def test_app_task_progress_requires_approval_of_current_plan(client, fake_db, auth_headers):
    card = make_pipeline()
    case, version = _ready_solution(card)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[case]), FakeResult(rows=[version])])
    response = await client.patch(_path(card), headers=auth_headers, json={"expectedVersion": 1, "status": "in_progress"})
    assert response.status_code == 409
    assert fake_db.commit_count == 0
