"""Hosted MCP transport for OAuth-authenticated Build operations."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.types import ASGIApp

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.mcp_oauth import MCP_SCOPE, oauth_provider
from app.models import Pipeline, SignalAnalysisCase, SolutionCase
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
OFFLINE_ACCESS_SCOPE = "offline_access"

_auth_settings = AuthSettings(
    issuer_url=settings.mcp_issuer_url,
    resource_server_url=settings.mcp_public_url,
    service_documentation_url=f"{settings.web_app_url.rstrip('/')}/mcp",
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
        valid_scopes=[MCP_SCOPE, OFFLINE_ACCESS_SCOPE],
        default_scopes=[MCP_SCOPE, OFFLINE_ACCESS_SCOPE],
    ),
    revocation_options=RevocationOptions(enabled=True),
    required_scopes=[MCP_SCOPE],
)

hosted_mcp_server = MCPServer(
    name="immensity-build",
    version="0.5.0",
    website_url=settings.web_app_url,
    auth_server_provider=oauth_provider,
    auth=_auth_settings,
    instructions=(
        "Start with list_projects, then select_project when more than one project is available. "
        "For Build design, use get_problem_case and get_build_workspace. "
        "Use expected_version on every Build edit so concurrent website or IDE edits cannot be overwritten. "
        "Approve the finished Build workspace before implementation. For implementation, start with "
        "get_build_contract, then fetch only the documents needed for the current task. Use get_next_task "
        "and respect dependencies. Report blockers instead of changing approved scope. Submit verification "
        "before marking work done; never claim an unrun check passed."
    ),
)


async def _access_for_call(db) -> IdeAccess:
    token = get_access_token()
    if token is None or not token.subject:
        raise ValueError("Sign in to Immensity before using this tool")
    claims = token.claims or {}
    pipeline_id = claims.get("selected_pipeline_id")
    if pipeline_id:
        card = (
            await db.execute(
                select(Pipeline).where(
                    Pipeline.id == pipeline_id,
                    Pipeline.user_id == token.subject,
                    Pipeline.removed_at == None,
                )
            )
        ).scalar_one_or_none()
        if card is None:
            raise ValueError("The selected project is no longer available. Call list_projects and select_project.")
    else:
        cards = list(
            (
                await db.execute(
                    select(Pipeline)
                    .where(Pipeline.user_id == token.subject, Pipeline.removed_at == None)
                    .order_by(Pipeline.updated_at.desc())
                    .limit(2)
                )
            ).scalars().all()
        )
        if not cards:
            raise ValueError("No Immensity projects are available for this account")
        if len(cards) > 1:
            raise ValueError("Select a project first with list_projects and select_project")
        pipeline_id = str(cards[0].id)
        await oauth_provider.select_project_for_token(token.token, token.subject, pipeline_id)
    return IdeAccess.from_values(
        access_id=str(claims.get("token_id") or "oauth"),
        pipeline_id=str(pipeline_id),
        user_id=token.subject,
    )


async def _call(handler, *args):
    async with AsyncSessionLocal() as db:
        try:
            access = await _access_for_call(db)
            result = await handler(access.pipeline_id, *args, db, access)
            return jsonable_encoder(result)
        except HTTPException as exc:
            await db.rollback()
            detail = exc.detail if isinstance(exc.detail, str) else "Immensity request failed"
            raise ValueError(detail) from exc
        except Exception:
            await db.rollback()
            raise


@hosted_mcp_server.tool(description="List the Immensity projects available to the signed-in account.")
async def list_projects() -> dict[str, Any]:
    token = get_access_token()
    if token is None or not token.subject:
        raise ValueError("Sign in to Immensity before using this tool")
    async with AsyncSessionLocal() as db:
        claims = token.claims or {}
        cards = list(
            (
                await db.execute(
                    select(Pipeline)
                    .where(Pipeline.user_id == token.subject, Pipeline.removed_at == None)
                    .order_by(Pipeline.updated_at.desc())
                )
            ).scalars().all()
        )
        ids = [str(card.id) for card in cards]
        signal_rows = (
            await db.execute(
                select(SignalAnalysisCase).where(
                    SignalAnalysisCase.user_id == token.subject,
                    SignalAnalysisCase.pipeline_id.in_(ids),
                )
            )
        ).scalars().all() if ids else []
        solution_rows = (
            await db.execute(
                select(SolutionCase).where(
                    SolutionCase.user_id == token.subject,
                    SolutionCase.pipeline_id.in_(ids),
                )
            )
        ).scalars().all() if ids else []
        analysis = {str(row.pipeline_id): row.status for row in signal_rows}
        builds = {str(row.pipeline_id): row.status for row in solution_rows}
        selected = claims.get("selected_pipeline_id")
        return {
            "projects": [
                {
                    "id": str(card.id),
                    "name": card.project_name or card.name,
                    "stage": card.stage,
                    "analysisStatus": analysis.get(str(card.id), "not_started"),
                    "buildStatus": builds.get(str(card.id), "not_started"),
                    "selected": str(card.id) == selected,
                }
                for card in cards
            ]
        }


@hosted_mcp_server.tool(description="Select which Immensity project subsequent tools should use.")
async def select_project(project_id: str) -> dict[str, Any]:
    token = get_access_token()
    if token is None or not token.subject:
        raise ValueError("Sign in to Immensity before using this tool")
    async with AsyncSessionLocal() as db:
        card = (
            await db.execute(
                select(Pipeline).where(
                    Pipeline.id == project_id,
                    Pipeline.user_id == token.subject,
                    Pipeline.removed_at == None,
                )
            )
        ).scalar_one_or_none()
        if card is None:
            raise ValueError("Project not found")
    await oauth_provider.select_project_for_token(token.token, token.subject, project_id)
    return {"projectId": project_id, "name": card.project_name or card.name, "selected": True}


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
hosted_mcp_app: ASGIApp = _mcp_transport
