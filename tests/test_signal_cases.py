from datetime import datetime, timedelta, timezone

from conftest import FakeResult, TEST_UID, make_pipeline
from app.models import ClusterItem, SignalAnalysisCase, SignalAnalysisJob, SignalAnalysisVersion
from app.services.signal_cases import (
    build_current_case_document,
    build_empty_case_document,
    new_signal_case_and_job,
    signal_source_fingerprint,
    signal_source_updated_at,
)


def _item(item_id: str, *, body: str, at: datetime) -> ClusterItem:
    return ClusterItem(
        id=item_id,
        title=f"Evidence {item_id}",
        body=body,
        content_hash=f"hash-{body}",
        posted_at=at,
        scraped_at=at + timedelta(minutes=5),
        score=10,
        num_comments=2,
    )


def test_source_fingerprint_is_order_independent_and_tracks_material_changes():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    first = _item("a", body="one", at=now)
    second = _item("b", body="two", at=now)

    original = signal_source_fingerprint([first, second])

    assert signal_source_fingerprint([second, first]) == original
    second.body = "changed"
    assert signal_source_fingerprint([first, second]) != original


def test_source_updated_at_uses_latest_observation():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    first = _item("a", body="one", at=now)
    second = _item("b", body="two", at=now + timedelta(days=1))

    assert signal_source_updated_at([second, first]) == second.scraped_at


def test_new_case_and_job_are_linked_and_queued():
    card = make_pipeline()

    case, job = new_signal_case_and_job(card, TEST_UID)

    assert case.pipeline_id == card.id
    assert case.user_id == TEST_UID
    assert case.status == "queued"
    assert job.case_id == case.id
    assert job.kind == "initial"
    assert job.status == "queued"


def test_empty_case_document_is_frontend_ready():
    card = make_pipeline(name="Late invoices", project_name="Invoice helper", post_ids=["a", "b"])
    case, _ = new_signal_case_and_job(card, TEST_UID)

    payload = build_empty_case_document(card, case).model_dump(mode="json", by_alias=True)

    assert payload["version"] == 0
    assert payload["status"] == "queued"
    assert payload["project"]["projectName"] == "Invoice helper"
    assert payload["metrics"]["evidenceCount"] == 2
    assert payload["progress"]["step"] == "queued"


async def test_get_case_lazily_initializes_existing_project(client, fake_db, auth_headers):
    card = make_pipeline(name="Late invoices")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[]),
    ])

    response = await client.get(f"/pipeline/{card.id}/signal/case", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert any(isinstance(row, SignalAnalysisCase) for row in fake_db.added)
    assert any(isinstance(row, SignalAnalysisJob) for row in fake_db.added)


async def test_get_case_rejects_other_users_project(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])

    response = await client.get("/pipeline/not-owned/signal/case", headers=auth_headers)

    assert response.status_code == 404
    assert not any(isinstance(row, SignalAnalysisCase) for row in fake_db.added)


async def test_refresh_is_idempotent_while_a_job_is_active(client, fake_db, auth_headers):
    card = make_pipeline(name="Late invoices")
    case, active_job = new_signal_case_and_job(card, TEST_UID)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[case]),
        FakeResult(rows=[active_job]),
    ])

    response = await client.post(
        f"/pipeline/{card.id}/signal/case/refresh",
        headers=auth_headers,
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert not [
        row
        for row in fake_db.added
        if isinstance(row, SignalAnalysisJob) and row.id != active_job.id
    ]


def test_current_version_overlays_project_rename_and_lifecycle_state():
    card = make_pipeline(name="Late invoices", project_name="Cashflow")
    case, _ = new_signal_case_and_job(card, TEST_UID)
    case.status = "failed"
    case.safe_error = "Refresh failed. The previous analysis is still available."
    generated_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
    version = SignalAnalysisVersion(
        id="version-1",
        case_id=case.id,
        version=1,
        schema_version="signal-case.v1",
        provider="test",
        model="test",
        source_fingerprint="sha256:test",
        generated_at=generated_at,
        analysis=_stored_case_payload(card.id),
    )

    payload = build_current_case_document(card, case, version).model_dump(mode="json", by_alias=True)

    assert payload["status"] == "failed"
    assert payload["safeError"].startswith("Refresh failed")
    assert payload["project"]["projectName"] == "Cashflow"
    assert payload["version"] == 1


def _stored_case_payload(pipeline_id: str) -> dict:
    return {
        "version": 1,
        "status": "ready",
        "progress": None,
        "safeError": None,
        "project": {
            "pipelineId": pipeline_id,
            "projectName": "Old name",
            "clusterName": "Late invoices",
            "sourceFingerprint": "sha256:test",
            "analyzedAt": "2026-07-27T00:00:00+00:00",
            "sourceUpdatedAt": "2026-07-26T00:00:00+00:00",
        },
        "metrics": {
            "signalStrength": 0.5,
            "momentum30d": None,
            "freshnessDays": 1,
            "evidenceCount": 1,
            "authorCount": 1,
            "sourceDiversity": 1,
        },
        "thesis": None,
        "claims": [{
            "id": "claim-1",
            "text": "Invoices are late.",
            "kind": "observed",
            "confidence": "medium",
            "evidenceIds": ["evidence-1"],
            "confirmed": False,
            "rejected": False,
        }],
        "problemUnits": [],
        "audiences": [],
        "alternatives": [],
        "assumptions": [],
        "evidence": [{
            "id": "evidence-1",
            "title": "Late invoice",
            "excerpt": "My client is late.",
            "body": None,
            "platform": "reddit",
            "community": "freelance",
            "author": None,
            "observedAt": None,
            "score": 3,
            "commentCount": 1,
            "sourceUrl": None,
            "stance": "supporting",
            "claimIds": ["claim-1"],
            "problemUnitIds": [],
            "relevanceReason": None,
            "pinned": False,
            "userNote": None,
        }],
        "recommendedFocus": None,
    }
