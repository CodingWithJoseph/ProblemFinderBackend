from app.services.solution_worker import build_curated_problem_payload
from app.signal_contract import SignalCaseDocument


def _problem_case() -> SignalCaseDocument:
    return SignalCaseDocument.model_validate({
        "version": 3,
        "status": "ready",
        "project": {
            "pipelineId": "pipeline-1",
            "projectName": "Support workspace",
            "clusterName": "Support complaints",
            "sourceFingerprint": "fingerprint",
            "analyzedAt": "2026-08-21T10:00:00+00:00",
            "sourceUpdatedAt": "2026-08-20T10:00:00+00:00",
        },
        "metrics": {
            "freshnessDays": 1,
            "evidenceCount": 3,
            "authorCount": 3,
            "sourceDiversity": 1,
        },
        "problemUnits": [
            {
                "id": "problem-1",
                "title": "Included problem",
                "description": "Users cannot complete the workflow.",
                "kind": "core_problem",
                "evidenceIds": ["evidence-1", "evidence-2"],
                "evidenceCount": 2,
                "confidence": "medium",
                "affectedUsers": ["Support agent"],
                "situation": "During ticket triage",
                "relatedProblemIds": ["problem-2"],
            },
            {
                "id": "problem-2",
                "title": "Excluded problem",
                "description": "This should not shape Build.",
                "kind": "core_problem",
                "evidenceIds": ["evidence-3"],
                "evidenceCount": 1,
                "confidence": "medium",
                "affectedUsers": ["Manager"],
                "situation": "During reporting",
                "rejected": True,
            },
        ],
        "evidence": [
            {
                "id": "evidence-1",
                "title": "Kept source",
                "excerpt": "The workflow fails during triage.",
                "stance": "supporting",
                "problemUnitIds": ["problem-1"],
            },
            {
                "id": "evidence-2",
                "title": "Removed source",
                "excerpt": "This source was removed by the user.",
                "stance": "excluded",
                "problemUnitIds": ["problem-1"],
            },
            {
                "id": "evidence-3",
                "title": "Source for rejected problem",
                "excerpt": "This problem was rejected by the user.",
                "stance": "supporting",
                "problemUnitIds": ["problem-2"],
            },
        ],
    })


def test_build_receives_only_included_problems_and_sources():
    payload = build_curated_problem_payload(_problem_case())

    assert [unit["id"] for unit in payload["problemUnits"]] == ["problem-1"]
    assert payload["problemUnits"][0]["evidenceIds"] == ["evidence-1"]
    assert payload["problemUnits"][0]["evidenceCount"] == 1
    assert payload["problemUnits"][0]["relatedProblemIds"] == []
    assert [record["id"] for record in payload["evidence"]] == ["evidence-1"]
