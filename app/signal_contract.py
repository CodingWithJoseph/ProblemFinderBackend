"""Typed, versioned contract for the Signal analysis workspace.

The model-generated document is intentionally separate from the persistence
models.  It is validated before a version can become current, so malformed
output or dangling citations never replaces the last usable analysis.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


SIGNAL_ANALYSIS_SCHEMA_VERSION = "signal-case.v1"

SignalAnalysisStatus = Literal[
    "queued",
    "generating",
    "ready",
    "stale",
    "insufficient_evidence",
    "failed",
]
SignalClaimKind = Literal["observed", "inferred", "user_confirmed"]
SignalEvidenceStance = Literal["supporting", "contradictory", "ambiguous", "excluded"]
SignalProblemUnitKind = Literal["cause", "core_problem", "symptom", "consequence", "workaround"]
SignalConfidence = Literal["low", "medium", "high"]


class SignalContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class SignalProjectContext(SignalContractModel):
    pipeline_id: str = Field(min_length=1)
    project_name: str = Field(min_length=1)
    cluster_name: str | None = None
    source_fingerprint: str | None = None
    analyzed_at: str | None = None
    source_updated_at: str | None = None


class SignalAnalysisProgress(SignalContractModel):
    step: Literal[
        "queued",
        "preparing_evidence",
        "analyzing_problem",
        "mapping_context",
        "validating_citations",
        "saving",
    ]
    label: str = Field(min_length=1, max_length=160)


class SignalMetricSnapshot(SignalContractModel):
    signal_strength: float | None = Field(default=None, ge=0, le=1)
    momentum_30d: float | None = Field(default=None, alias="momentum30d")
    freshness_days: int | None = Field(default=None, ge=0)
    evidence_count: int = Field(ge=0)
    author_count: int | None = Field(default=None, ge=0)
    source_diversity: int | None = Field(default=None, ge=0)


class SignalClaim(SignalContractModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: SignalClaimKind
    confidence: SignalConfidence
    evidence_ids: list[str] = Field(default_factory=list)
    confirmed: bool = False
    rejected: bool = False


class SignalThesis(SignalContractModel):
    statement: str = Field(min_length=1)
    audience: str | None = None
    context: str | None = None
    core_problem: str = Field(min_length=1)
    consequence: str | None = None
    workaround: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    confirmed: bool = False


class SignalProblemUnit(SignalContractModel):
    id: str = Field(min_length=1)
    parent_id: str | None = None
    title: str = Field(min_length=1)
    description: str | None = None
    kind: SignalProblemUnitKind
    audience_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_count: int = Field(ge=0)
    source_diversity: int | None = Field(default=None, ge=0)
    frequency: SignalConfidence | None = None
    intensity: SignalConfidence | None = None
    momentum_30d: float | None = Field(default=None, alias="momentum30d")
    confidence: SignalConfidence
    pinned: bool = False
    rejected: bool = False


class SignalAudience(SignalContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: SignalClaimKind
    language: list[str] = Field(default_factory=list)
    communities: list[str] = Field(default_factory=list)
    reach_channels: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class SignalAlternative(SignalContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: Literal["manual", "software", "service", "avoidance", "none"]
    reason_used: str | None = None
    weakness: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class SignalAssumption(SignalContractModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    category: Literal["problem", "audience", "behavior", "access", "willingness_to_pay", "feasibility"]
    evidence_strength: SignalConfidence
    evidence_ids: list[str] = Field(default_factory=list)
    resolution_evidence: str = Field(min_length=1)
    problem_unit_id: str | None = None
    resolved: bool = False


class SignalEvidenceRecord(SignalContractModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    body: str | None = None
    platform: str | None = None
    community: str | None = None
    author: str | None = None
    observed_at: str | None = None
    score: int | None = None
    comment_count: int | None = Field(default=None, ge=0)
    source_url: str | None = None
    stance: SignalEvidenceStance
    claim_ids: list[str] = Field(default_factory=list)
    problem_unit_ids: list[str] = Field(default_factory=list)
    relevance_reason: str | None = None
    pinned: bool = False
    user_note: str | None = None


class SignalRecommendedFocus(SignalContractModel):
    problem_unit_id: str | None = None
    title: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    supported: list[str] = Field(default_factory=list)
    risky: list[str] = Field(default_factory=list)
    suggested_validation_step: str = Field(min_length=1)


class SignalAnalysisModelOutput(SignalContractModel):
    """Schema-constrained content returned by the analysis model.

    Project context, metrics and evidence text are intentionally excluded. They
    come from authoritative database rows when the final case is assembled.
    """

    thesis: SignalThesis | None
    claims: list[SignalClaim]
    problem_units: list[SignalProblemUnit]
    audiences: list[SignalAudience]
    alternatives: list[SignalAlternative]
    assumptions: list[SignalAssumption]
    recommended_focus: SignalRecommendedFocus | None


class SignalCitation(SignalContractModel):
    evidence_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=160)


class SignalProposal(SignalContractModel):
    id: str = Field(min_length=1)
    kind: Literal[
        "revise_thesis",
        "create_problem_unit",
        "update_problem_unit",
        "merge_problem_units",
        "create_audience",
        "create_assumption",
        "link_evidence",
        "validation_handoff",
    ]
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "accepted", "rejected"] = "pending"
    target_kind: str | None = None
    target_id: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class SignalConversationModelOutput(SignalContractModel):
    text: str = Field(min_length=1)
    citations: list[SignalCitation] = Field(default_factory=list)
    proposal: SignalProposal | None = None
    insufficient_evidence: bool = False


class SignalConversationSummary(SignalContractModel):
    id: str
    title: str
    updated_at: str
    archived: bool


class SignalConversationTurnResponse(SignalContractModel):
    id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: str
    citations: list[SignalCitation] = Field(default_factory=list)
    proposal: SignalProposal | None = None
    insufficient_evidence: bool = False


class SignalConversationResponse(SignalContractModel):
    id: str
    title: str
    turns: list[SignalConversationTurnResponse] = Field(default_factory=list)


class SignalCaseDocument(SignalContractModel):
    """The complete document served to the three Signal workspace screens."""

    version: int = Field(ge=0)
    status: SignalAnalysisStatus
    progress: SignalAnalysisProgress | None = None
    safe_error: str | None = None
    project: SignalProjectContext
    metrics: SignalMetricSnapshot
    thesis: SignalThesis | None = None
    claims: list[SignalClaim] = Field(default_factory=list)
    problem_units: list[SignalProblemUnit] = Field(default_factory=list)
    audiences: list[SignalAudience] = Field(default_factory=list)
    alternatives: list[SignalAlternative] = Field(default_factory=list)
    assumptions: list[SignalAssumption] = Field(default_factory=list)
    evidence: list[SignalEvidenceRecord] = Field(default_factory=list)
    recommended_focus: SignalRecommendedFocus | None = None

    @model_validator(mode="after")
    def validate_references(self) -> "SignalCaseDocument":
        evidence_ids = _unique_ids(self.evidence, "evidence")
        claim_ids = _unique_ids(self.claims, "claim")
        unit_ids = _unique_ids(self.problem_units, "problem unit")
        audience_ids = _unique_ids(self.audiences, "audience")

        for claim in self.claims:
            _require_known(claim.evidence_ids, evidence_ids, f"claim {claim.id} evidence")
            if claim.kind != "user_confirmed" and not claim.evidence_ids:
                raise ValueError(f"claim {claim.id} must cite at least one evidence record")

        if self.thesis:
            _require_known(self.thesis.claim_ids, claim_ids, "thesis claims")

        for unit in self.problem_units:
            if unit.parent_id is not None and unit.parent_id not in unit_ids:
                raise ValueError(f"problem unit {unit.id} has unknown parent {unit.parent_id}")
            if unit.parent_id == unit.id:
                raise ValueError(f"problem unit {unit.id} cannot parent itself")
            _require_known(unit.audience_ids, audience_ids, f"problem unit {unit.id} audiences")
            _require_known(unit.claim_ids, claim_ids, f"problem unit {unit.id} claims")
            _require_known(unit.evidence_ids, evidence_ids, f"problem unit {unit.id} evidence")
            if unit.evidence_count != len(set(unit.evidence_ids)):
                raise ValueError(f"problem unit {unit.id} evidenceCount must match its cited evidence")

        for audience in self.audiences:
            _require_known(audience.evidence_ids, evidence_ids, f"audience {audience.id} evidence")

        for alternative in self.alternatives:
            _require_known(alternative.evidence_ids, evidence_ids, f"alternative {alternative.id} evidence")

        for assumption in self.assumptions:
            _require_known(assumption.evidence_ids, evidence_ids, f"assumption {assumption.id} evidence")
            if assumption.problem_unit_id is not None and assumption.problem_unit_id not in unit_ids:
                raise ValueError(
                    f"assumption {assumption.id} has unknown problem unit {assumption.problem_unit_id}"
                )

        for record in self.evidence:
            _require_known(record.claim_ids, claim_ids, f"evidence {record.id} claims")
            _require_known(record.problem_unit_ids, unit_ids, f"evidence {record.id} problem units")

        if self.recommended_focus and self.recommended_focus.problem_unit_id is not None:
            if self.recommended_focus.problem_unit_id not in unit_ids:
                raise ValueError(
                    "recommended focus points to unknown problem unit "
                    f"{self.recommended_focus.problem_unit_id}"
                )

        if self.status == "ready" and (not self.claims or not self.evidence):
            raise ValueError("ready Signal cases require claims and evidence")
        return self


def _unique_ids(rows: list[SignalContractModel], label: str) -> set[str]:
    ids = [str(getattr(row, "id")) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} ids must be unique")
    return set(ids)


def _require_known(values: list[str], known: set[str], label: str) -> None:
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"{label} contains unknown ids: {', '.join(unknown)}")
