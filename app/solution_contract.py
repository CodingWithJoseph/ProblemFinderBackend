"""Versioned, schema-constrained contract for the MVP Build Warroom."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


SOLUTION_SCHEMA_VERSION = "solution-case.v1"

SolutionStatus = Literal["not_started", "queued", "generating", "ready", "failed"]
DeliveryTarget = Literal["web", "ios", "android"]


class SolutionContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class SolutionProgress(SolutionContractModel):
    step: Literal[
        "queued",
        "preparing_context",
        "designing_solution",
        "validating_traceability",
        "saving",
    ]
    label: str = Field(min_length=1, max_length=160)


class SolutionProjectContext(SolutionContractModel):
    pipeline_id: str = Field(min_length=1)
    project_name: str = Field(min_length=1)
    delivery_target: DeliveryTarget
    generated_at: str | None = None


class SolutionPrimaryFlow(SolutionContractModel):
    actor: str = Field(min_length=1, max_length=160)
    trigger: str = Field(min_length=1, max_length=300)
    steps: list[str] = Field(min_length=1, max_length=8)
    success_outcome: str = Field(min_length=1, max_length=300)


class SolutionFeature(SolutionContractModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=600)
    problem_unit_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)


class SolutionComponent(SolutionContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(min_length=1, max_length=600)
    feature_ids: list[str] = Field(default_factory=list)


class SolutionDataEntity(SolutionContractModel):
    name: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=500)
    fields: list[str] = Field(default_factory=list, max_length=20)


class SolutionApiContract(SolutionContractModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=240)
    purpose: str = Field(min_length=1, max_length=500)
    feature_ids: list[str] = Field(default_factory=list)


class SolutionArchitecture(SolutionContractModel):
    stack: list[str] = Field(min_length=1, max_length=12)
    components: list[SolutionComponent] = Field(min_length=1, max_length=12)
    data_model: list[SolutionDataEntity] = Field(default_factory=list, max_length=20)
    api_contracts: list[SolutionApiContract] = Field(default_factory=list, max_length=20)
    integrations: list[str] = Field(default_factory=list, max_length=12)
    security_and_privacy: list[str] = Field(default_factory=list, max_length=12)


class SolutionTask(SolutionContractModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=800)
    feature_ids: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(min_length=1, max_length=8)
    status: Literal["todo", "in_progress", "blocked", "done"] = "todo"


class SolutionTestPlan(SolutionContractModel):
    unit: list[str] = Field(default_factory=list, max_length=20)
    integration: list[str] = Field(default_factory=list, max_length=20)
    end_to_end: list[str] = Field(min_length=1, max_length=12)


class SolutionDeliveryItem(SolutionContractModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=240)
    kind: Literal["build", "test", "configuration", "deployment", "store_submission"]
    required: bool = True
    status: Literal["not_started", "in_progress", "complete", "blocked"] = "not_started"


class SolutionModelOutput(SolutionContractModel):
    mvp_goal: str = Field(min_length=1, max_length=600)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    non_goals: list[str] = Field(min_length=1, max_length=20)
    primary_flow: SolutionPrimaryFlow
    features: list[SolutionFeature] = Field(min_length=1, max_length=5)
    architecture: SolutionArchitecture
    tasks: list[SolutionTask] = Field(min_length=1, max_length=30)
    test_plan: SolutionTestPlan
    delivery_checklist: list[SolutionDeliveryItem] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_internal_references(self) -> "SolutionModelOutput":
        feature_ids = _unique_ids(self.features, "feature")
        task_ids = _unique_ids(self.tasks, "task")
        component_ids = _unique_ids(self.architecture.components, "component")
        _unique_ids(self.delivery_checklist, "delivery item")
        if not component_ids:
            raise ValueError("at least one architecture component is required")

        for component in self.architecture.components:
            _require_known(component.feature_ids, feature_ids, f"component {component.id} features")
        for api in self.architecture.api_contracts:
            _require_known(api.feature_ids, feature_ids, f"API {api.method} {api.path} features")
        for task in self.tasks:
            _require_known(task.feature_ids, feature_ids, f"task {task.id} features")
            _require_known(task.depends_on, task_ids, f"task {task.id} dependencies")
            if task.id in task.depends_on:
                raise ValueError(f"task {task.id} cannot depend on itself")
        _require_acyclic_tasks(self.tasks)
        return self


class SolutionCaseDocument(SolutionContractModel):
    version: int = Field(ge=0)
    status: SolutionStatus
    progress: SolutionProgress | None = None
    safe_error: str | None = None
    project: SolutionProjectContext
    problem_case_version: int = Field(ge=0)
    mvp_goal: str | None = Field(default=None, max_length=600)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    non_goals: list[str] = Field(default_factory=list, max_length=20)
    primary_flow: SolutionPrimaryFlow | None = None
    features: list[SolutionFeature] = Field(default_factory=list, max_length=5)
    architecture: SolutionArchitecture | None = None
    tasks: list[SolutionTask] = Field(default_factory=list, max_length=30)
    test_plan: SolutionTestPlan | None = None
    delivery_checklist: list[SolutionDeliveryItem] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_ready_document(self) -> "SolutionCaseDocument":
        if self.status != "ready":
            return self
        SolutionModelOutput.model_validate({
            "mvpGoal": self.mvp_goal,
            "constraints": self.constraints,
            "nonGoals": self.non_goals,
            "primaryFlow": self.primary_flow,
            "features": self.features,
            "architecture": self.architecture,
            "tasks": self.tasks,
            "testPlan": self.test_plan,
            "deliveryChecklist": self.delivery_checklist,
        })
        if self.version == 0 or self.problem_case_version == 0:
            raise ValueError("ready solution cases require generated versions")
        return self


class SolutionGenerateRequest(SolutionContractModel):
    delivery_target: DeliveryTarget = "web"
    constraints: list[str] = Field(default_factory=list, max_length=12)
    preferred_stack: list[str] = Field(default_factory=list, max_length=12)


def _unique_ids(rows: list[SolutionContractModel], label: str) -> set[str]:
    ids = [str(getattr(row, "id")) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} ids must be unique")
    return set(ids)


def _require_known(values: list[str], known: set[str], label: str) -> None:
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"{label} contains unknown ids: {', '.join(unknown)}")


def _require_acyclic_tasks(tasks: list[SolutionTask]) -> None:
    dependencies = {task.id: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("task dependencies must be acyclic")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)
