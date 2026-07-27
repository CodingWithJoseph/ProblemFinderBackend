from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import SignalConversation, SignalConversationTurn
from app.routes import signal_workspace
from app.services.signal_cases import (
    apply_signal_overrides,
    validate_signal_override_patch,
)
from app.signal_contract import SignalCaseDocument, SignalConversationModelOutput


def _case() -> SignalCaseDocument:
    return SignalCaseDocument.model_validate({
        "version": 1,
        "status": "ready",
        "progress": None,
        "safeError": None,
        "project": {
            "pipelineId": "pipeline-1",
            "projectName": "Invoice helper",
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
    })


def test_overrides_apply_without_mutating_generated_document():
    original = _case()
    override = SimpleNamespace(
        object_kind="evidence",
        object_id="evidence-1",
        patch={"pinned": True, "userNote": "Use this in interviews."},
    )

    updated = apply_signal_overrides(original, [override])

    assert original.evidence[0].pinned is False
    assert updated.evidence[0].pinned is True
    assert updated.evidence[0].user_note == "Use this in interviews."


def test_override_rejects_generated_link_mutation():
    with pytest.raises(ValueError, match="Unsupported override fields"):
        validate_signal_override_patch(
            "claim",
            {"evidenceIds": ["evidence-2"]},
        )


async def test_ask_signal_returns_grounded_model_response(monkeypatch):
    class Provider:
        async def ask(self, **kwargs):
            return SignalConversationModelOutput.model_validate({
                "text": "The evidence directly reports a late invoice.",
                "citations": [{"evidenceId": "evidence-1", "label": "Late invoice"}],
                "proposal": None,
                "insufficientEvidence": False,
            })

    monkeypatch.setattr(signal_workspace, "get_signal_analysis_provider", lambda settings: Provider())

    output = await signal_workspace._ask_signal_model(
        _case(),
        [],
        "What do we actually know?",
    )

    assert output.insufficient_evidence is False
    assert output.citations[0].evidence_id == "evidence-1"


async def test_ask_signal_rejects_invented_citation(monkeypatch):
    class Provider:
        async def ask(self, **kwargs):
            return SignalConversationModelOutput.model_validate({
                "text": "Unsupported answer.",
                "citations": [{"evidenceId": "invented", "label": "Unknown"}],
                "proposal": None,
                "insufficientEvidence": False,
            })

    monkeypatch.setattr(signal_workspace, "get_signal_analysis_provider", lambda settings: Provider())

    output = await signal_workspace._ask_signal_model(
        _case(),
        [],
        "What do we actually know?",
    )

    assert output.insufficient_evidence is True
    assert output.citations == []
    assert "invalid result" in output.text


def test_conversation_response_restores_persisted_proposal_shape():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    conversation = SignalConversation(
        id="conversation-1",
        case_id="case-1",
        user_id="user-1",
        title="What do we know?",
        created_at=now,
        updated_at=now,
    )
    turn = SignalConversationTurn(
        id="turn-1",
        conversation_id=conversation.id,
        role="assistant",
        text="Confirm this framing.",
        citations=[{"evidenceId": "evidence-1", "label": "Late invoice"}],
        proposal={
            "id": "proposal-1",
            "kind": "revise_thesis",
            "title": "Revise the thesis",
            "summary": "Make the late-payment context explicit.",
            "evidenceIds": ["evidence-1"],
            "status": "pending",
            "targetKind": "thesis",
            "targetId": "thesis",
            "changes": {"context": "After work is delivered"},
        },
        insufficient_evidence=False,
        created_at=now,
    )

    payload = signal_workspace._conversation_response(
        conversation,
        [turn],
    ).model_dump(mode="json", by_alias=True)

    assert payload["turns"][0]["proposal"]["targetKind"] == "thesis"
    assert payload["turns"][0]["citations"][0]["evidenceId"] == "evidence-1"

