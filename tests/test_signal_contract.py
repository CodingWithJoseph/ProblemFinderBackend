import pytest
from pydantic import ValidationError

from app.signal_contract import SIGNAL_ANALYSIS_SCHEMA_VERSION, SignalCaseDocument


def _document() -> dict:
    return {
        "version": 1,
        "status": "ready",
        "progress": None,
        "safeError": None,
        "project": {
            "pipelineId": "pipeline-1",
            "projectName": "Invoice reliability",
            "clusterName": "Late freelance invoices",
            "sourceFingerprint": "sha256:example",
            "analyzedAt": "2026-07-27T10:00:00+00:00",
            "sourceUpdatedAt": "2026-07-26T10:00:00+00:00",
        },
        "metrics": {
            "signalStrength": 0.78,
            "momentum30d": 12.5,
            "freshnessDays": 1,
            "evidenceCount": 1,
            "authorCount": 1,
            "sourceDiversity": 1,
        },
        "thesis": {
            "statement": "Freelancers lose time chasing overdue invoices.",
            "audience": "Independent freelancers",
            "context": "After client work is delivered",
            "coreProblem": "Payment follow-up is manual and unreliable.",
            "consequence": "Cash flow becomes unpredictable.",
            "workaround": "Repeated reminders",
            "claimIds": ["claim-1"],
            "confirmed": False,
        },
        "claims": [{
            "id": "claim-1",
            "text": "Freelancers repeatedly chase overdue invoices.",
            "kind": "observed",
            "confidence": "high",
            "evidenceIds": ["evidence-1"],
            "confirmed": False,
            "rejected": False,
        }],
        "problemUnits": [{
            "id": "unit-1",
            "parentId": None,
            "title": "Manual payment follow-up",
            "description": "The freelancer has no dependable collection workflow.",
            "kind": "core_problem",
            "audienceIds": ["audience-1"],
            "claimIds": ["claim-1"],
            "evidenceIds": ["evidence-1"],
            "evidenceCount": 1,
            "sourceDiversity": 1,
            "frequency": "high",
            "intensity": "medium",
            "momentum30d": 12.5,
            "confidence": "high",
            "pinned": False,
            "rejected": False,
        }],
        "audiences": [{
            "id": "audience-1",
            "name": "Independent freelancers",
            "description": "Solo service providers who invoice clients.",
            "kind": "observed",
            "language": ["chasing invoices"],
            "communities": ["freelance"],
            "reachChannels": ["professional communities"],
            "evidenceIds": ["evidence-1"],
            "unknowns": ["Which accounting tools are already used?"],
        }],
        "alternatives": [],
        "assumptions": [{
            "id": "assumption-1",
            "question": "How often do invoices become overdue?",
            "whyItMatters": "Frequency determines whether a workflow product is valuable.",
            "category": "behavior",
            "evidenceStrength": "medium",
            "evidenceIds": ["evidence-1"],
            "resolutionEvidence": "Interview ten freelancers and inspect invoice histories.",
            "problemUnitId": "unit-1",
            "resolved": False,
        }],
        "evidence": [{
            "id": "evidence-1",
            "title": "Client is 45 days late",
            "excerpt": "I spend hours every month chasing the same invoices.",
            "body": None,
            "platform": "reddit",
            "community": "freelance",
            "author": "user-1",
            "observedAt": "2026-07-26T10:00:00+00:00",
            "score": 23,
            "commentCount": 8,
            "sourceUrl": "https://example.com/post",
            "stance": "supporting",
            "claimIds": ["claim-1"],
            "problemUnitIds": ["unit-1"],
            "relevanceReason": "Direct report of the workflow problem.",
            "pinned": False,
            "userNote": None,
        }],
        "recommendedFocus": {
            "problemUnitId": "unit-1",
            "title": "Validate invoice follow-up frequency",
            "rationale": "The pain is evidenced, but frequency is not yet quantified.",
            "supported": ["Manual follow-up occurs"],
            "risky": ["Frequency across the audience"],
            "suggestedValidationStep": "Interview ten freelancers.",
        },
    }


def test_contract_accepts_frontend_camel_case_and_serializes_the_same_shape():
    case = SignalCaseDocument.model_validate(_document())

    payload = case.model_dump(mode="json", by_alias=True)

    assert SIGNAL_ANALYSIS_SCHEMA_VERSION == "signal-case.v1"
    assert payload["project"]["pipelineId"] == "pipeline-1"
    assert payload["problemUnits"][0]["evidenceCount"] == 1
    assert payload["recommendedFocus"]["problemUnitId"] == "unit-1"


def test_contract_rejects_uncited_generated_claim():
    payload = _document()
    payload["claims"][0]["evidenceIds"] = []

    with pytest.raises(ValidationError, match="must cite at least one evidence"):
        SignalCaseDocument.model_validate(payload)


def test_contract_rejects_dangling_evidence_reference():
    payload = _document()
    payload["claims"][0]["evidenceIds"] = ["missing"]

    with pytest.raises(ValidationError, match="unknown ids: missing"):
        SignalCaseDocument.model_validate(payload)


def test_contract_rejects_ready_case_without_evidence():
    payload = _document()
    payload["claims"] = []
    payload["thesis"] = None
    payload["problemUnits"] = []
    payload["audiences"] = []
    payload["assumptions"] = []
    payload["evidence"] = []
    payload["recommendedFocus"] = None

    with pytest.raises(ValidationError, match="ready Signal cases require"):
        SignalCaseDocument.model_validate(payload)


def test_contract_allows_empty_queued_case():
    payload = _document()
    payload.update({
        "version": 0,
        "status": "queued",
        "progress": {"step": "queued", "label": "Waiting to analyze this Signal"},
        "thesis": None,
        "claims": [],
        "problemUnits": [],
        "audiences": [],
        "alternatives": [],
        "assumptions": [],
        "evidence": [],
        "recommendedFocus": None,
    })

    case = SignalCaseDocument.model_validate(payload)

    assert case.status == "queued"
    assert case.version == 0
