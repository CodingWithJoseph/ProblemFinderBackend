from conftest import FakeResult, TEST_UID, make_pipeline
from types import SimpleNamespace

from mcp.server.auth.provider import AccessToken
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from starlette.requests import Request
import pytest

import app.mcp_oauth as oauth_module
from app.models import SignalAnalysisVersion, SolutionVersion
from app.routes.ide_workspace import (
    _ready_workspace,
    TaskProgressRequest,
    require_ide_access,
    update_ide_build_section,
)
from app.services.signal_cases import new_signal_case_and_job
from app.solution_contract import SolutionSectionUpdateRequest
from test_solution_workspace import _ready_solution


async def test_ide_rest_access_accepts_oauth_access_token(monkeypatch):
    async def load_access_token(token: str):
        assert token == "oauth-token"
        return AccessToken(
            token=token,
            client_id="client-1",
            scopes=["mcp:use"],
            resource=oauth_module.oauth_provider.settings.mcp_public_url,
            subject=TEST_UID,
            claims={"token_id": "oauth-token-id"},
        )

    monkeypatch.setattr(oauth_module.oauth_provider, "load_access_token", load_access_token)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="oauth-token")

    access = await require_ide_access("project-1", request, credentials)

    assert access.id == "oauth-token-id"
    assert access.pipeline_id == "project-1"
    assert access.user_id == TEST_UID
    assert request.state.uid == TEST_UID


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


async def test_ide_build_edit_creates_a_version_the_website_can_render(fake_db):
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
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),
            FakeResult(rows=[problem_case]),
            FakeResult(rows=[problem_version]),
            FakeResult(rows=[solution_case]),
            FakeResult(rows=[solution_version]),
        ],
        scalar=[1],
    )

    document = await update_ide_build_section(
        str(card.id),
        "constraints",
        SolutionSectionUpdateRequest(
            expected_version=1,
            value=["Use the existing authentication system"],
        ),
        fake_db,
        SimpleNamespace(user_id=TEST_UID),
    )

    assert document.version == 2
    assert document.constraints == ["Use the existing authentication system"]
    saved = next(row for row in fake_db.added if isinstance(row, SolutionVersion))
    assert saved.provider == "ide"
    assert saved.document["constraints"] == ["Use the existing authentication system"]
    assert solution_case.current_version_id == saved.id
