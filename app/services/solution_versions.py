"""Shared writes and runtime views for the canonical Build version."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.build_documents import (
    ensure_build_documents,
    render_build_documents,
    replace_build_document,
)
from app.models import Pipeline, SolutionCase, SolutionTaskState, SolutionVersion
from app.services.solution_cases import build_current_solution_document, utc_now
from app.solution_contract import (
    SOLUTION_SCHEMA_VERSION,
    SolutionCaseDocument,
    SolutionFeature,
    SolutionSectionKey,
)


async def solution_with_task_states(
    db: AsyncSession,
    card: Pipeline,
    case: SolutionCase,
    version: SolutionVersion,
) -> SolutionCaseDocument:
    document = build_current_solution_document(card, case, version)
    rows = (await db.execute(
        select(SolutionTaskState).where(SolutionTaskState.case_id == case.id)
    )).scalars().all()
    statuses = {row.task_id: row.status for row in rows}
    if not statuses:
        return document
    return document.model_copy(update={
        "tasks": [
            task.model_copy(update={"status": statuses.get(task.id, task.status)})
            for task in document.tasks
        ]
    })


def replace_solution_section(
    current: SolutionCaseDocument,
    key: SolutionSectionKey,
    value: Any,
    *,
    next_version: int,
) -> SolutionCaseDocument:
    payload = current.model_dump(mode="json", by_alias=False)
    payload.update({
        "version": next_version,
        "approval_status": "draft",
        "approved_version": None,
        "approved_at": None,
        "documents": [],
    })
    if key == "delivery_targets":
        payload["project"]["delivery_targets"] = value
    elif key == "mvp_goal" and isinstance(value, dict):
        payload["mvp_goal"] = value.get("mvpGoal", value.get("mvp_goal"))
        payload["primary_users"] = value.get("primaryUsers", value.get("primary_users", []))
        payload["mvp_capability"] = value.get(
            "requiredCapability",
            value.get("required_capability", value.get("mvpCapability", value.get("mvp_capability"))),
        )
        payload["mvp_outcome"] = value.get(
            "successfulOutcome",
            value.get("successful_outcome", value.get("mvpOutcome", value.get("mvp_outcome"))),
        )
    elif key == "features":
        try:
            features = [
                SolutionFeature.model_validate(item).model_dump(mode="json", by_alias=False)
                for item in value
            ]
        except (TypeError, ValidationError) as exc:
            raise HTTPException(
                status_code=422,
                detail="The Build section is not valid: features must be a valid list",
            ) from exc
        if not features:
            raise HTTPException(
                status_code=422,
                detail="Keep at least one feature in the Build.",
            )

        feature_ids = {feature["id"] for feature in features}
        architecture = payload.get("architecture")
        if isinstance(architecture, dict):
            for component in architecture.get("components", []):
                component["feature_ids"] = [
                    feature_id
                    for feature_id in component.get("feature_ids", [])
                    if feature_id in feature_ids
                ]
            for contract in architecture.get("api_contracts", []):
                contract["feature_ids"] = [
                    feature_id
                    for feature_id in contract.get("feature_ids", [])
                    if feature_id in feature_ids
                ]

        tasks = []
        for task in payload.get("tasks", []):
            task["feature_ids"] = [
                feature_id
                for feature_id in task.get("feature_ids", [])
                if feature_id in feature_ids
            ]
            if task["feature_ids"]:
                tasks.append(task)
        if payload.get("tasks") and not tasks:
            raise HTTPException(
                status_code=422,
                detail="Keep a feature used by at least one task, or update the tasks first.",
            )
        task_ids = {task["id"] for task in tasks}
        for task in tasks:
            task["depends_on"] = [
                task_id
                for task_id in task.get("depends_on", [])
                if task_id in task_ids
            ]

        payload["features"] = features
        payload["tasks"] = tasks
    else:
        payload[key] = value

    try:
        updated = SolutionCaseDocument.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"The Build section is not valid: {exc.errors()[0]['msg']}",
        ) from exc

    rendered = render_build_documents(updated)
    previous_decisions = next(
        (item.content for item in ensure_build_documents(current).documents if item.key == "decisions"),
        None,
    )
    if previous_decisions is not None:
        rendered = [
            item.model_copy(update={"content": previous_decisions})
            if item.key == "decisions"
            else item
            for item in rendered
        ]
    return updated.model_copy(update={"documents": rendered})


async def save_solution_section_version(
    db: AsyncSession,
    card: Pipeline,
    case: SolutionCase,
    current: SolutionVersion,
    key: SolutionSectionKey,
    value: Any,
    *,
    expected_version: int,
    provider: str,
    model: str,
) -> SolutionCaseDocument:
    if current.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail="The Build Contract changed. Reload before saving your edit.",
        )
    next_version = int(await db.scalar(
        select(func.coalesce(func.max(SolutionVersion.version), 0)).where(
            SolutionVersion.case_id == case.id
        )
    ) or 0) + 1
    document = replace_solution_section(
        build_current_solution_document(card, case, current),
        key,
        value,
        next_version=next_version,
    )
    now = utc_now()
    version = SolutionVersion(
        id=str(uuid.uuid4()),
        case_id=str(case.id),
        version=next_version,
        schema_version=SOLUTION_SCHEMA_VERSION,
        provider=provider,
        model=model,
        problem_case_version=document.problem_case_version,
        document=document.model_dump(mode="json", by_alias=True),
        generated_at=now,
    )
    db.add(version)
    await db.flush()
    case.current_version_id = version.id
    case.approval_status = "draft"
    case.approved_version_id = None
    case.approved_at = None
    case.updated_at = now
    await db.execute(delete(SolutionTaskState).where(SolutionTaskState.case_id == case.id))
    await db.commit()
    return build_current_solution_document(card, case, version)


async def save_solution_document_version(
    db: AsyncSession,
    card: Pipeline,
    case: SolutionCase,
    current: SolutionVersion,
    document_key: str,
    content: str,
    *,
    expected_version: int,
    provider: str,
    model: str,
) -> SolutionCaseDocument:
    if current.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail="The Build Contract changed. Reload before saving your edit.",
        )
    next_version = int(await db.scalar(
        select(func.coalesce(func.max(SolutionVersion.version), 0)).where(
            SolutionVersion.case_id == case.id
        )
    ) or 0) + 1
    try:
        document = replace_build_document(
            build_current_solution_document(card, case, current),
            document_key,
            content.strip() + "\n",
            next_version=next_version,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Build document not found") from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=409,
            detail="This agent instruction file is generated from the Build Contract",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"The edited document is not structurally valid: {exc}",
        ) from exc

    now = utc_now()
    version = SolutionVersion(
        id=str(uuid.uuid4()),
        case_id=str(case.id),
        version=next_version,
        schema_version=SOLUTION_SCHEMA_VERSION,
        provider=provider,
        model=model,
        problem_case_version=document.problem_case_version,
        document=document.model_dump(mode="json", by_alias=True),
        generated_at=now,
    )
    db.add(version)
    await db.flush()
    case.current_version_id = version.id
    case.approval_status = "draft"
    case.approved_version_id = None
    case.approved_at = None
    case.updated_at = now
    await db.execute(delete(SolutionTaskState).where(SolutionTaskState.case_id == case.id))
    await db.commit()
    return build_current_solution_document(card, case, version)


async def approve_solution_version(
    db: AsyncSession,
    card: Pipeline,
    case: SolutionCase,
    current: SolutionVersion,
    *,
    expected_version: int,
) -> SolutionCaseDocument:
    if current.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail="The Build Contract changed. Reload before approving it.",
        )
    now = utc_now()
    case.approval_status = "approved"
    case.approved_version_id = current.id
    case.approved_at = now
    case.updated_at = now
    await db.commit()
    return build_current_solution_document(card, case, current)
