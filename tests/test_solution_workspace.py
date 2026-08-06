from conftest import FakeResult, TEST_UID, make_pipeline
from app.models import SignalAnalysisVersion, SolutionCase, SolutionJob
from app.services.signal_cases import new_signal_case_and_job


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
