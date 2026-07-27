from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from conftest import FakeSession, TEST_UID, make_pipeline
from app.models import ClusterItem
from app.services.signal_analysis_worker import (
    build_case_document,
    fail_signal_job,
    select_representative_evidence,
)
from app.services.signal_cases import new_signal_case_and_job
from app.signal_contract import SignalAnalysisModelOutput


def _item(
    item_id: str,
    *,
    score: int,
    observed_at: datetime,
) -> ClusterItem:
    return ClusterItem(
        id=item_id,
        pipeline_version="v2",
        platform="reddit",
        community="freelance",
        title=f"Evidence {item_id}",
        body=f"Body {item_id}",
        author=f"author-{item_id}",
        score=score,
        num_comments=score // 2,
        posted_at=observed_at,
        scraped_at=observed_at,
        content_hash=f"hash-{item_id}",
    )


def _output(evidence_id: str) -> SignalAnalysisModelOutput:
    return SignalAnalysisModelOutput.model_validate({
        "thesis": None,
        "claims": [{
            "id": "claim-1",
            "text": "Freelancers manually chase late invoices.",
            "kind": "observed",
            "confidence": "medium",
            "evidenceIds": [evidence_id],
            "confirmed": False,
            "rejected": False,
        }],
        "problemUnits": [{
            "id": "unit-1",
            "parentId": None,
            "title": "Manual follow-up",
            "description": None,
            "kind": "core_problem",
            "audienceIds": [],
            "claimIds": ["claim-1"],
            "evidenceIds": [evidence_id],
            "evidenceCount": 1,
            "sourceDiversity": 1,
            "frequency": "medium",
            "intensity": "medium",
            "momentum30d": None,
            "confidence": "medium",
            "pinned": False,
            "rejected": False,
        }],
        "audiences": [],
        "alternatives": [],
        "assumptions": [],
        "recommendedFocus": {
            "problemUnitId": "unit-1",
            "title": "Validate follow-up frequency",
            "rationale": "Frequency remains uncertain.",
            "supported": ["Manual follow-up occurs"],
            "risky": ["How often it occurs"],
            "suggestedValidationStep": "Interview freelancers.",
        },
    })


def test_representative_selection_balances_engagement_and_recency():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    items = [
        _item("high-old", score=100, observed_at=now - timedelta(days=100)),
        _item("mid-old", score=80, observed_at=now - timedelta(days=90)),
        _item("low-new", score=1, observed_at=now),
        _item("low-newer", score=2, observed_at=now - timedelta(hours=1)),
    ]

    selected = select_representative_evidence(items, max_items=3)
    ids = {str(item.id) for item in selected}

    assert "high-old" in ids
    assert "low-new" in ids
    assert len(ids) == 3


def test_case_assembly_rejects_model_citation_not_in_selected_evidence():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    card = make_pipeline(name="Late invoices")
    case, _ = new_signal_case_and_job(card, TEST_UID)
    item = _item("evidence-1", score=10, observed_at=now)

    with pytest.raises(ValidationError, match="unknown ids: missing"):
        build_case_document(
            card,
            case,
            [item],
            [item],
            None,
            _output("missing"),
        )


def test_case_assembly_uses_authoritative_evidence_content():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    card = make_pipeline(name="Late invoices")
    case, _ = new_signal_case_and_job(card, TEST_UID)
    item = _item("evidence-1", score=10, observed_at=now)

    document = build_case_document(
        card,
        case,
        [item],
        [item],
        None,
        _output("evidence-1"),
    )

    assert document.status == "ready"
    assert document.evidence[0].title == "Evidence evidence-1"
    assert document.evidence[0].claim_ids == ["claim-1"]
    assert document.metrics.evidence_count == 1


async def test_failed_refresh_preserves_current_version_pointer():
    db = FakeSession()
    card = make_pipeline()
    case, job = new_signal_case_and_job(card, TEST_UID, kind="refresh")
    case.current_version_id = "last-good-version"
    job.status = "running"

    await fail_signal_job(
        db,
        job.id,
        "timeout",
        job=job,
        case=case,
    )

    assert case.current_version_id == "last-good-version"
    assert case.status == "failed"
    assert "previous version" in case.safe_error
    assert job.status == "failed"
    assert job.error_category == "timeout"
