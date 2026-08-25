"""Hosted MCP transport for the existing project-scoped Build operations."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.routes.ide_workspace import (
    IdeAccess,
    TaskProgressRequest,
    TaskVerification,
    approve_ide_build_workspace,
    generate_ide_build_workspace,
    get_ide_build_document,
    get_ide_build_workspace,
    get_ide_problem_case,
    get_ide_project_context,
    get_ide_project_status,
    get_next_ide_task,
    list_ide_build_documents,
    resolve_ide_access_token,
    update_ide_build_document,
    update_ide_build_section,
    update_ide_task,
)
from app.solution_contract import (
    ApproveSolutionRequest,
    BuildDocumentUpdateRequest,
    SolutionGenerateRequest,
    SolutionSectionUpdateRequest,
)

settings = get_settings()
current_access: ContextVar[IdeAccess | None] = ContextVar("mcp_ide_access", default=None)

hosted_mcp_server = MCPServer(
    name="immensity-build",
    version="0.4.0",
    instructions=(
        "For Build design, start with get_problem_case and get_build_workspace. "
        "Use expected_version on every Build edit so concurrent website or IDE edits cannot be overwritten. "
        "Approve the finished Build workspace before implementation. For implementation, start with "
        "get_build_contract, then fetch only the documents needed for the current task. Use get_next_task "
        "and respect dependencies. Report blockers instead of changing approved scope. Submit verification "
        "before marking work done; never claim an unrun check passed."
    ),
)


async def _call(handler, *args):
    access = current_access.get()
    if access is None:
        raise ValueError("Missing project access token")
    async with AsyncSessionLocal() as db:
        try:
            result = await handler(access.pipeline_id, *args, db, access)
            return jsonable_encoder(result)
        except HTTPException as exc:
            await db.rollback()
            detail = exc.detail if isinstance(exc.detail, str) else "Immensity request failed"
            raise ValueError(detail) from exc
        except Exception:
            await db.rollback()
            raise


@hosted_mcp_server.tool(description="Return the analyzed Problem Case used as the source for Build design.")
async def get_problem_case() -> dict[str, Any]:
    return await _call(get_ide_problem_case)


@hosted_mcp_server.tool(description="Return the current Build workspace shown in Immensity.")
async def get_build_workspace() -> dict[str, Any]:
    return await _call(get_ide_build_workspace)


@hosted_mcp_server.tool(description="Generate a Build draft from the current analyzed Problem Case.")
async def generate_build_workspace(
    delivery_targets: list[Literal["web", "ios", "android"]] | None = None,
    constraints: list[str] | None = None,
    preferred_stack: list[str] | None = None,
) -> dict[str, Any]:
    body = SolutionGenerateRequest(
        delivery_targets=delivery_targets or ["web"],
        constraints=constraints or [],
        preferred_stack=preferred_stack or [],
    )
    return await _call(generate_ide_build_workspace, body)


@hosted_mcp_server.tool(description="Replace one structured Build section with optimistic version checking.")
async def update_build_section(
    section: Literal[
        "mvp_goal",
        "delivery_targets",
        "constraints",
        "non_goals",
        "primary_flow",
        "features",
        "architecture",
        "tasks",
        "test_plan",
        "delivery_checklist",
    ],
    value: Any,
    expected_version: int,
) -> dict[str, Any]:
    return await _call(
        update_ide_build_section,
        section,
        SolutionSectionUpdateRequest(value=value, expected_version=expected_version),
    )


@hosted_mcp_server.tool(description="Replace an editable Build markdown document.")
async def update_build_document(
    document: Literal["build_contract", "features", "requirements", "tasks", "test_plan", "decisions"],
    content: str,
    expected_version: int,
) -> dict[str, Any]:
    return await _call(
        update_ide_build_document,
        document,
        BuildDocumentUpdateRequest(content=content, expected_version=expected_version),
    )


@hosted_mcp_server.tool(description="Approve and freeze the current Build workspace version.")
async def approve_build_workspace(expected_version: int) -> dict[str, Any]:
    return await _call(
        approve_ide_build_workspace,
        ApproveSolutionRequest(expected_version=expected_version),
    )


@hosted_mcp_server.tool(description="Return the approved Build Contract. Call this before implementation.")
async def get_build_contract() -> dict[str, Any]:
    return await _call(get_ide_build_document, "build_contract")


@hosted_mcp_server.tool(description="List the focused documents in the approved Build package.")
async def list_build_documents() -> dict[str, Any]:
    return await _call(list_ide_build_documents)


@hosted_mcp_server.tool(description="Fetch one approved Build document by key.")
async def get_build_document(
    document: Literal[
        "features",
        "requirements",
        "tasks",
        "test_plan",
        "decisions",
        "agents",
        "claude",
        "copilot_instructions",
    ],
) -> dict[str, Any]:
    return await _call(get_ide_build_document, document)


@hosted_mcp_server.tool(description="Return Build version, task counts, completion state, and blockers.")
async def get_project_status() -> dict[str, Any]:
    return await _call(get_ide_project_status)


@hosted_mcp_server.tool(description="Return the next todo task whose dependencies are complete.")
async def get_next_task() -> dict[str, Any]:
    return await _call(get_next_ide_task)


@hosted_mcp_server.tool(description="Move a task to todo or in progress and attach an optional note.")
async def update_task_progress(
    task_id: str,
    status: Literal["todo", "in_progress"],
    note: str | None = None,
) -> dict[str, Any]:
    return await _call(update_ide_task, task_id, TaskProgressRequest(status=status, note=note))


@hosted_mcp_server.tool(description="Mark a task blocked with the concrete reason.")
async def report_blocker(task_id: str, note: str) -> dict[str, Any]:
    return await _call(update_ide_task, task_id, TaskProgressRequest(status="blocked", note=note))


@hosted_mcp_server.tool(description="Mark a task done with the checks that actually passed.")
async def submit_verification(
    task_id: str,
    summary: str,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    verification = TaskVerification.model_validate({"summary": summary, "checks": checks})
    return await _call(
        update_ide_task,
        task_id,
        TaskProgressRequest(
            status="done",
            note=summary,
            verification=verification,
        ),
    )


@hosted_mcp_server.tool(description="Compatibility view containing the analyzed problem and approved Build data.")
async def get_project_context() -> dict[str, Any]:
    return await _call(get_ide_project_context)


_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=settings.mcp_allowed_hosts_list,
    allowed_origins=settings.origins_list,
)
_mcp_transport = hosted_mcp_server.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security,
)


class ProjectTokenAuth:
    """Authenticate one project token before the MCP transport handles a request."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        authorization = next(
            (
                value.decode("latin-1")
                for key, value in scope.get("headers", [])
                if key.lower() == b"authorization"
            ),
            "",
        )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            await JSONResponse(
                {"detail": "Missing project access token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        async with AsyncSessionLocal() as db:
            try:
                access = await resolve_ide_access_token(token.strip(), db)
            except HTTPException:
                await JSONResponse(
                    {"detail": "Invalid or expired project access token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return

        access_context = current_access.set(access)
        try:
            await self.app(scope, receive, send)
        finally:
            current_access.reset(access_context)


hosted_mcp_app: ASGIApp = ProjectTokenAuth(_mcp_transport)
