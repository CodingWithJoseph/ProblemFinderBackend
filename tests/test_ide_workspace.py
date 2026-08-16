from conftest import FakeResult, TEST_UID, make_pipeline

from app.models import IdeAccessToken, SignalAnalysisVersion
from app.routes.ide_workspace import _ready_workspace, hash_access_token, mint_access_token
from app.routes.ide_workspace import TaskProgressRequest
from app.services.signal_cases import new_signal_case_and_job
from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from test_solution_workspace import _ready_solution


def test_minted_ide_token_is_prefixed_and_only_persisted_as_a_hash():
    cleartext, digest, prefix = mint_access_token()

    assert cleartext.startswith("imm_mcp_")
    assert prefix == cleartext[:16]
    assert digest == hash_access_token(cleartext)
    assert cleartext not in digest
    assert len(digest) == 64


async def test_create_ide_token_is_project_scoped_and_returns_secret_once(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.post(
        f"/pipeline/{card.id}/ide/access-tokens",
        headers=auth_headers,
        json={"name": "Cursor"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "Cursor"
    assert payload["token"].startswith("imm_mcp_")
    stored = next(row for row in fake_db.added if isinstance(row, IdeAccessToken))
    assert stored.pipeline_id == card.id
    assert stored.token_hash == hash_access_token(payload["token"])
    assert payload["token"] != stored.token_hash
    assert fake_db.commit_count == 1


def test_done_task_requires_actual_verification_checks():
    with pytest.raises(ValidationError, match="verification summary and checks"):
        TaskProgressRequest.model_validate({"status": "done", "note": "looks good"})

    request = TaskProgressRequest.model_validate({
        "status": "done",
        "note": "Endpoint check passed",
        "verification": {
            "summary": "Endpoint check passed",
            "checks": [{"command": "pytest tests/test_api.py", "outcome": "passed"}],
        },
    })
    assert request.verification is not None
    assert request.verification.checks[0].outcome == "passed"

    with pytest.raises(ValidationError, match="every submitted check to pass"):
        TaskProgressRequest.model_validate({
            "status": "done",
            "verification": {
                "summary": "One check failed",
                "checks": [{"command": "pytest", "outcome": "failed"}],
            },
        })


def test_blocked_task_requires_a_concrete_note():
    with pytest.raises(ValidationError, match="blocked tasks require a note"):
        TaskProgressRequest.model_validate({"status": "blocked"})


async def test_coding_agent_access_is_pinned_to_the_approved_current_version(fake_db):
    card = make_pipeline()
    problem_case, _ = new_signal_case_and_job(card, TEST_UID)
    problem_case.current_version_id = "problem-version-1"
    problem_version = SignalAnalysisVersion(
        id="problem-version-1",
        case_id=problem_case.id,
        version=1,
        schema_version="signal-case.v1",
        provider="test",
        model="test",
        source_fingerprint="fingerprint",
        analysis={},
    )
    solution_case, solution_version = _ready_solution(card)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[problem_case]),
        FakeResult(rows=[problem_version]),
        FakeResult(rows=[solution_case]),
        FakeResult(rows=[solution_version]),
    ])

    with pytest.raises(HTTPException, match="Approve the current Build Contract"):
        await _ready_workspace(fake_db, str(card.id), TEST_UID)

    solution_case.approval_status = "approved"
    solution_case.approved_version_id = solution_version.id
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[problem_case]),
        FakeResult(rows=[problem_version]),
        FakeResult(rows=[solution_case]),
        FakeResult(rows=[solution_version]),
    ])
    _, _, ready_case, ready_version = await _ready_workspace(fake_db, str(card.id), TEST_UID)

    assert ready_case.id == solution_case.id
    assert ready_version.id == solution_case.approved_version_id
