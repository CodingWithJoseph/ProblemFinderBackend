from conftest import FakeResult, TEST_UID, make_pipeline
from datetime import datetime, timezone

from app.models import SignalAnalysisVersion, SolutionCase, SolutionJob, SolutionVersion
from app.services.signal_cases import new_signal_case_and_job
from test_solution_contract import _document


def _ready_solution(card):
    now = datetime.now(timezone.utc)
    version = SolutionVersion(
        id="solution-version-1",
        case_id="solution-case-1",
        version=1,
        schema_version="solution-case.v1",
        provider="test",
        model="test",
        problem_case_version=2,
        document=_document(),
        generated_at=now,
    )
    case = SolutionCase(
        id="solution-case-1",
        pipeline_id=card.id,
        user_id=TEST_UID,
        status="ready",
        problem_case_version=2,
        request={"deliveryTarget": "web", "constraints": [], "preferredStack": []},
        current_version_id=version.id,
        approval_status="draft",
        approved_version_id=None,
        created_at=now,
        updated_at=now,
    )
    return case, version


async def test_get_solution_case_returns_not_started_for_owned_project(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[])])

    response = await client.get(f"/pipeline/{card.id}/solution/case", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "not_started"
    assert response.json()["project"]["deliveryTarget"] == "web"


async def test_generate_solution_queues_case_from_ready_problem_breakdown(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    signal_case, _ = new_signal_case_and_job(card, TEST_UID)
    signal_case.current_version_id = "problem-version-2"
    problem_version = SignalAnalysisVersion(
        id="problem-version-2",
        case_id=signal_case.id,
        version=2,
        schema_version="signal-case.v1",
        provider="test",
        model="test",
        source_fingerprint="fingerprint",
        analysis={},
    )
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[signal_case]),
        FakeResult(rows=[problem_version]),
        FakeResult(rows=[]),
    ])

    response = await client.post(
        f"/pipeline/{card.id}/solution/case/generate",
        headers=auth_headers,
        json={
            "deliveryTarget": "web",
            "constraints": ["Ship in 72 hours"],
            "preferredStack": ["Next.js", "FastAPI"],
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["problemCaseVersion"] == 2
    assert any(isinstance(row, SolutionCase) for row in fake_db.added)
    assert any(isinstance(row, SolutionJob) for row in fake_db.added)


async def test_generate_solution_requires_ready_problem_breakdown(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[])])

    response = await client.post(
        f"/pipeline/{card.id}/solution/case/generate",
        headers=auth_headers,
        json={"deliveryTarget": "web", "constraints": [], "preferredStack": []},
    )

    assert response.status_code == 409


async def test_get_ready_solution_backfills_the_focused_document_package(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    case, version = _ready_solution(card)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[case]),
        FakeResult(rows=[version]),
    ])

    response = await client.get(f"/pipeline/{card.id}/solution/case", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["approvalStatus"] == "draft"
    assert response.json()["documents"][0]["filename"] == "docs/BUILD_CONTRACT.md"


async def test_editing_a_build_document_creates_an_immutable_draft_version(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    case, version = _ready_solution(card)
    fake_db.stub(
        execute=[FakeResult(rows=[card]), FakeResult(rows=[case]), FakeResult(rows=[version])],
        scalar=[1],
    )

    response = await client.patch(
        f"/pipeline/{card.id}/solution/case/documents/requirements",
        headers=auth_headers,
        json={
            "expectedVersion": 1,
            "content": (
                "# Requirements\n\n"
                "| ID | Feature | Requirement |\n"
                "| --- | --- | --- |\n"
                "| REQ-feature-1-1 | feature-1 | Reminder is verified |\n"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["approvalStatus"] == "draft"
    saved = next(row for row in fake_db.added if isinstance(row, SolutionVersion))
    assert saved.provider == "user"
    assert version.document["version"] == 1
    assert case.current_version_id == saved.id


async def test_approval_pins_the_exact_current_build_version(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    case, version = _ready_solution(card)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[case]),
        FakeResult(rows=[version]),
    ])

    response = await client.post(
        f"/pipeline/{card.id}/solution/case/approve",
        headers=auth_headers,
        json={"expectedVersion": 1},
    )

    assert response.status_code == 200
    assert response.json()["approvalStatus"] == "approved"
    assert response.json()["approvedVersion"] == 1
    assert case.approved_version_id == version.id


async def test_build_package_export_is_a_zip_download(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline(project_name="Invoice helper")
    case, version = _ready_solution(card)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[case]),
        FakeResult(rows=[version]),
    ])

    response = await client.get(
        f"/pipeline/{card.id}/solution/case/export",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "invoice-helper-build-v1.zip" in response.headers["content-disposition"]
    assert response.content.startswith(b"PK")
