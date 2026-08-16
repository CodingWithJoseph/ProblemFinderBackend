"""Project-scoped IDE access and MCP-facing build context endpoints."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import (
    IdeAccessToken,
    Pipeline,
    SignalAnalysisCase,
    SignalAnalysisVersion,
    SolutionCase,
    SolutionTaskState,
    SolutionVersion,
)
from app.services.solution_cases import build_current_solution_document


router = APIRouter(tags=["ide"])
ide_bearer = HTTPBearer(auto_error=False)
TOKEN_LIFETIME = timedelta(days=30)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_access_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mint_access_token() -> tuple[str, str, str]:
    cleartext = f"imm_mcp_{secrets.token_urlsafe(32)}"
    return cleartext, hash_access_token(cleartext), cleartext[:16]


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CreateIdeTokenRequest(ApiModel):
    name: str = Field(default="My IDE", min_length=1, max_length=120)


class IdeTokenSummary(ApiModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class CreatedIdeToken(IdeTokenSummary):
    token: str


class VerificationCheck(ApiModel):
    command: str = Field(min_length=1, max_length=500)
    outcome: Literal["passed", "failed", "skipped"]
    details: str | None = Field(default=None, max_length=2000)


class TaskVerification(ApiModel):
    summary: str = Field(min_length=1, max_length=2000)
    checks: list[VerificationCheck] = Field(min_length=1, max_length=20)


class TaskProgressRequest(ApiModel):
    status: Literal["todo", "in_progress", "blocked", "done"]
    note: str | None = Field(default=None, max_length=2000)
    verification: TaskVerification | None = None

    @model_validator(mode="after")
    def require_status_evidence(self) -> "TaskProgressRequest":
        if self.status == "blocked" and not (self.note or "").strip():
            raise ValueError("blocked tasks require a note")
        if self.status == "done":
            if self.verification is None:
                raise ValueError("done tasks require a verification summary and checks")
            if any(check.outcome != "passed" for check in self.verification.checks):
                raise ValueError("done tasks require every submitted check to pass")
        return self


class IdeAccess:
    def __init__(self, row: IdeAccessToken):
        self.id = str(row.id)
        self.pipeline_id = str(row.pipeline_id)
        self.user_id = row.user_id


def serialize_token(row: IdeAccessToken, *, cleartext: str | None = None) -> dict:
    payload = {
        "id": str(row.id),
        "name": row.name,
        "tokenPrefix": row.token_prefix,
        "createdAt": row.created_at,
        "expiresAt": row.expires_at,
        "lastUsedAt": row.last_used_at,
        "revokedAt": row.revoked_at,
    }
    if cleartext is not None:
        payload["token"] = cleartext
    return payload


async def require_ide_access(
    pipeline_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(ide_bearer),
    db: AsyncSession = Depends(get_db),
) -> IdeAccess:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing IDE access token")
    now = utc_now()
    row = (await db.execute(
        select(IdeAccessToken).where(
            IdeAccessToken.pipeline_id == pipeline_id,
            IdeAccessToken.token_hash == hash_access_token(credentials.credentials),
            IdeAccessToken.revoked_at == None,
            IdeAccessToken.expires_at > now,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired IDE access token")
    row.last_used_at = now
    request.state.uid = row.user_id
    request.state.ide_token_id = str(row.id)
    await db.commit()
    return IdeAccess(row)


async def _owned_card(db: AsyncSession, pipeline_id: str, uid: str) -> Pipeline:
    card = (await db.execute(
        select(Pipeline).where(
            Pipeline.id == pipeline_id,
            Pipeline.user_id == uid,
            Pipeline.removed_at == None,
        )
    )).scalar_one_or_none()
    if card is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return card


async def _ready_workspace(
    db: AsyncSession,
    pipeline_id: str,
    uid: str,
) -> tuple[Pipeline, SignalAnalysisVersion, SolutionCase, SolutionVersion]:
    card = await _owned_card(db, pipeline_id, uid)
    problem_case = (await db.execute(
        select(SignalAnalysisCase).where(
            SignalAnalysisCase.pipeline_id == pipeline_id,
            SignalAnalysisCase.user_id == uid,
        )
    )).scalar_one_or_none()
    problem_version = None
    if problem_case and problem_case.current_version_id:
        problem_version = (await db.execute(
            select(SignalAnalysisVersion).where(
                SignalAnalysisVersion.id == problem_case.current_version_id
            )
        )).scalar_one_or_none()

    solution_case = (await db.execute(
        select(SolutionCase).where(
            SolutionCase.pipeline_id == pipeline_id,
            SolutionCase.user_id == uid,
        )
    )).scalar_one_or_none()
    solution_version = None
    if solution_case and solution_case.current_version_id:
        solution_version = (await db.execute(
            select(SolutionVersion).where(SolutionVersion.id == solution_case.current_version_id)
        )).scalar_one_or_none()

    if problem_version is None or solution_case is None or solution_version is None or solution_case.status != "ready":
        raise HTTPException(
            status_code=409,
            detail="Complete Analysis and Build before connecting a coding agent",
        )
    if (
        getattr(solution_case, "approval_status", None) != "approved"
        or getattr(solution_case, "approved_version_id", None) != solution_version.id
    ):
        raise HTTPException(
            status_code=409,
            detail="Approve the current Build Contract before connecting a coding agent",
        )
    return card, problem_version, solution_case, solution_version


async def _task_states(db: AsyncSession, case_id: str) -> dict[str, SolutionTaskState]:
    rows = (await db.execute(
        select(SolutionTaskState).where(SolutionTaskState.case_id == case_id)
    )).scalars().all()
    return {row.task_id: row for row in rows}


def _solution_with_task_states(
    card: Pipeline,
    case: SolutionCase,
    version: SolutionVersion,
    states: dict[str, SolutionTaskState],
) -> dict:
    document = build_current_solution_document(card, case, version).model_dump(mode="json", by_alias=True)
    for task in document["tasks"]:
        state = states.get(task["id"])
        if state is not None:
            task["status"] = state.status
            task["progressNote"] = state.note
            task["verification"] = state.verification or None
    return document


@router.get("/pipeline/{pipeline_id}/ide/access-tokens")
async def list_ide_tokens(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _owned_card(db, pipeline_id, uid)
    rows = (await db.execute(
        select(IdeAccessToken)
        .where(IdeAccessToken.pipeline_id == pipeline_id, IdeAccessToken.user_id == uid)
        .order_by(IdeAccessToken.created_at.desc())
    )).scalars().all()
    return {"data": [serialize_token(row) for row in rows]}


@router.post(
    "/pipeline/{pipeline_id}/ide/access-tokens",
    response_model=CreatedIdeToken,
    status_code=status.HTTP_201_CREATED,
)
async def create_ide_token(
    pipeline_id: str,
    body: CreateIdeTokenRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _owned_card(db, pipeline_id, uid)
    cleartext, token_hash, prefix = mint_access_token()
    now = utc_now()
    row = IdeAccessToken(
        id=str(uuid.uuid4()),
        pipeline_id=pipeline_id,
        user_id=uid,
        name=body.name.strip(),
        token_hash=token_hash,
        token_prefix=prefix,
        created_at=now,
        expires_at=now + TOKEN_LIFETIME,
    )
    db.add(row)
    await db.commit()
    return serialize_token(row, cleartext=cleartext)


@router.delete("/pipeline/{pipeline_id}/ide/access-tokens/{token_id}", status_code=204)
async def revoke_ide_token(
    pipeline_id: str,
    token_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    row = (await db.execute(
        select(IdeAccessToken).where(
            IdeAccessToken.id == token_id,
            IdeAccessToken.pipeline_id == pipeline_id,
            IdeAccessToken.user_id == uid,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="IDE access token not found")
    row.revoked_at = utc_now()
    await db.commit()


@router.get("/ide/projects/{pipeline_id}/context")
async def get_ide_project_context(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    access: IdeAccess = Depends(require_ide_access),
):
    card, problem_version, solution_case, solution_version = await _ready_workspace(
        db, pipeline_id, access.user_id
    )
    states = await _task_states(db, str(solution_case.id))
    return {
        "schemaVersion": "ide-context.v1",
        "project": {
            "id": str(card.id),
            "name": card.project_name or card.name,
        },
        "problemCase": {
            "version": problem_version.version,
            "schemaVersion": problem_version.schema_version,
            "analysis": problem_version.analysis,
        },
        "solutionCase": _solution_with_task_states(card, solution_case, solution_version, states),
    }


@router.get("/ide/projects/{pipeline_id}/next-task")
async def get_next_ide_task(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    access: IdeAccess = Depends(require_ide_access),
):
    card, _, solution_case, solution_version = await _ready_workspace(db, pipeline_id, access.user_id)
    states = await _task_states(db, str(solution_case.id))
    solution = _solution_with_task_states(card, solution_case, solution_version, states)
    statuses = {task["id"]: task["status"] for task in solution["tasks"]}
    next_task = next(
        (
            task
            for task in solution["tasks"]
            if task["status"] == "todo"
            and all(statuses.get(dependency) == "done" for dependency in task["dependsOn"])
        ),
        None,
    )
    return {"data": next_task, "allComplete": all(value == "done" for value in statuses.values())}


@router.get("/ide/projects/{pipeline_id}/documents")
async def list_ide_build_documents(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    access: IdeAccess = Depends(require_ide_access),
):
    card, _, solution_case, solution_version = await _ready_workspace(
        db, pipeline_id, access.user_id
    )
    solution = build_current_solution_document(card, solution_case, solution_version)
    return {
        "version": solution.version,
        "approvalStatus": solution.approval_status,
        "data": [
            item.model_dump(mode="json", by_alias=True, exclude={"content"})
            for item in solution.documents
        ],
    }


@router.get("/ide/projects/{pipeline_id}/documents/{document_key}")
async def get_ide_build_document(
    pipeline_id: str,
    document_key: str,
    db: AsyncSession = Depends(get_db),
    access: IdeAccess = Depends(require_ide_access),
):
    card, _, solution_case, solution_version = await _ready_workspace(
        db, pipeline_id, access.user_id
    )
    solution = build_current_solution_document(card, solution_case, solution_version)
    document = next((item for item in solution.documents if item.key == document_key), None)
    if document is None:
        raise HTTPException(status_code=404, detail="Build document not found")
    return {
        "version": solution.version,
        "data": document.model_dump(mode="json", by_alias=True),
    }


@router.get("/ide/projects/{pipeline_id}/status")
async def get_ide_project_status(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    access: IdeAccess = Depends(require_ide_access),
):
    card, _, solution_case, solution_version = await _ready_workspace(
        db, pipeline_id, access.user_id
    )
    states = await _task_states(db, str(solution_case.id))
    solution = _solution_with_task_states(card, solution_case, solution_version, states)
    counts = {status_name: 0 for status_name in ("todo", "in_progress", "blocked", "done")}
    blocked = []
    for task in solution["tasks"]:
        counts[task["status"]] += 1
        if task["status"] == "blocked":
            blocked.append({
                "id": task["id"],
                "title": task["title"],
                "note": task.get("progressNote"),
            })
    return {
        "data": {
            "buildVersion": solution["version"],
            "approvalStatus": solution["approvalStatus"],
            "taskCounts": counts,
            "blockedTasks": blocked,
            "allComplete": bool(solution["tasks"]) and counts["done"] == len(solution["tasks"]),
        }
    }


@router.patch("/ide/projects/{pipeline_id}/tasks/{task_id}")
async def update_ide_task(
    pipeline_id: str,
    task_id: str,
    body: TaskProgressRequest,
    db: AsyncSession = Depends(get_db),
    access: IdeAccess = Depends(require_ide_access),
):
    card, _, solution_case, solution_version = await _ready_workspace(db, pipeline_id, access.user_id)
    solution = build_current_solution_document(card, solution_case, solution_version)
    if task_id not in {task.id for task in solution.tasks}:
        raise HTTPException(status_code=404, detail="Solution task not found")

    row = (await db.execute(
        select(SolutionTaskState).where(
            SolutionTaskState.case_id == solution_case.id,
            SolutionTaskState.task_id == task_id,
        )
    )).scalar_one_or_none()
    now = utc_now()
    if row is None:
        row = SolutionTaskState(
            id=str(uuid.uuid4()),
            case_id=str(solution_case.id),
            task_id=task_id,
            created_at=now,
        )
        db.add(row)
    row.status = body.status
    row.note = body.note
    row.verification = (
        body.verification.model_dump(mode="json", by_alias=True)
        if body.verification
        else {}
    )
    row.updated_by = f"ide:{access.id}"
    row.updated_at = now
    await db.commit()
    return {
        "data": {
            "taskId": task_id,
            "status": row.status,
            "note": row.note,
            "verification": row.verification or None,
            "updatedAt": row.updated_at,
        }
    }
