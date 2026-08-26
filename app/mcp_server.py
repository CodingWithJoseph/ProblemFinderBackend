"""Hosted MCP transport for OAuth-authenticated Build operations."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.types import ASGIApp

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.mcp_oauth import MCP_SCOPE, oauth_provider
from app.models import Pipeline, SignalAnalysisCase, SignalAnalysisVersion, SolutionCase
from app.prompts.signal_analysis import SIGNAL_ANALYSIS_INSTRUCTIONS
from app.routes.clusters import search_clusters
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
from app.routes.pipeline import WatchBody, get_pipeline_card, watch_cluster
from app.services.signal_analysis_worker import (
    build_analysis_payload,
    build_case_document,
    build_problem_model_output,
    load_signal_source_items,
    usable_signal_items,
)
from app.services.signal_cases import (
    find_signal_case,
    signal_source_fingerprint,
    signal_source_updated_at,
    utc_now,
)
from app.signal_contract import SIGNAL_ANALYSIS_SCHEMA_VERSION, SignalProblemAnalysisOutput
from app.solution_contract import (
    ApproveSolutionRequest,
    BuildDocumentUpdateRequest,
    SolutionGenerateRequest,
    SolutionSectionUpdateRequest,
)

settings = get_settings()
OFFLINE_ACCESS_SCOPE = "offline_access"
OPENID_SCOPE = "openid"
EMAIL_SCOPE = "email"
SUPPORTED_OAUTH_SCOPES = [MCP_SCOPE, OFFLINE_ACCESS_SCOPE, OPENID_SCOPE, EMAIL_SCOPE]

READ_ONLY_CLOSED = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    open_world_hint=False,
)
WRITE_CLOSED = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    open_world_hint=False,
)
DESTRUCTIVE_WRITE_CLOSED = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    open_world_hint=False,
)

_auth_settings = AuthSettings(
    issuer_url=settings.mcp_issuer_url,
    resource_server_url=settings.mcp_public_url,
    service_documentation_url=f"{settings.web_app_url.rstrip('/')}/mcp",
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
        valid_scopes=SUPPORTED_OAUTH_SCOPES,
        default_scopes=SUPPORTED_OAUTH_SCOPES,
    ),
    revocation_options=RevocationOptions(enabled=True),
    required_scopes=[MCP_SCOPE],
)

hosted_mcp_server = MCPServer(
    name="immensity-build",
    version="0.6.0",
    website_url=settings.web_app_url,
    auth_server_provider=oauth_provider,
    auth=_auth_settings,
    instructions=(
        "To create a project, search_problems first, then call create_project_from_problem with "
        "the chosen cluster ID. For analysis, call start_analysis with the returned project ID, "
        "retrieve every evidence page, analyze that evidence with the host model, and call "
        "save_problem_analysis with the exact sourceFingerprint and schema-conforming result. "
        "Immensity does not call its own analysis model on the MCP path. For existing projects, "
        "start with list_projects, then select_project when more than one is available. "
        "For Build design, use get_problem_case and get_build_workspace. "
        "Use expected_version on every Build edit so concurrent website or IDE edits cannot be overwritten. "
        "Approve the finished Build workspace before implementation. For implementation, start with "
        "get_build_contract, then fetch only the documents needed for the current task. Use get_next_task "
        "and respect dependencies. Report blockers instead of changing approved scope. Submit verification "
        "before marking work done; never claim an unrun check passed."
    ),
)

SEARCH_PROBLEM_LIMIT = 10
ANALYSIS_PAGE_DEFAULT = 50
ANALYSIS_PAGE_MAX = 100


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


def _authenticated_subject() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise ValueError("Sign in to Immensity before using this tool")
    return token.subject


def _tool_error(exc: HTTPException) -> ValueError:
    detail = exc.detail if isinstance(exc.detail, str) else "Immensity request failed"
    return ValueError(detail)


async def _owned_project(db, project_id: str, uid: str) -> Pipeline:
    card = (
        await db.execute(
            select(Pipeline).where(
                Pipeline.id == project_id,
                Pipeline.user_id == uid,
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()
    if card is None:
        raise ValueError("Pipeline card not found")
    return card


@hosted_mcp_server.tool(
    description=(
        "Search Immensity's existing problem clusters and return concise candidates "
        "that can be used with create_project_from_problem."
    ),
    annotations=READ_ONLY_CLOSED,
)
async def search_problems(query: str) -> dict[str, Any]:
    uid = _authenticated_subject()
    async with AsyncSessionLocal() as db:
        try:
            result = await search_clusters(
                q=query,
                limit=SEARCH_PROBLEM_LIMIT,
                offset=0,
                min_posts=1,
                db=db,
                uid=uid,
            )
            problems = [
                {
                    "clusterId": str(candidate["id"]),
                    "name": candidate["name"],
                    "problemStatement": candidate["problemStatement"],
                    "summary": candidate["summary"],
                    "evidenceCount": candidate["post_count"],
                    "sources": candidate["sources"],
                    "communities": candidate["subreddits"],
                    "hasProject": candidate["is_watched"],
                }
                for candidate in result["data"]
            ]
            return {
                "problems": problems,
                "returned": len(problems),
                "total": result["total"],
            }
        except HTTPException as exc:
            await db.rollback()
            raise _tool_error(exc) from exc
        except Exception:
            await db.rollback()
            raise


@hosted_mcp_server.tool(
    description=(
        "Create or return an Immensity project for the signed-in user from an existing "
        "problem cluster returned by search_problems."
    ),
    annotations=WRITE_CLOSED,
)
async def create_project_from_problem(cluster_id: str) -> dict[str, Any]:
    uid = _authenticated_subject()
    async with AsyncSessionLocal() as db:
        try:
            watched = await watch_cluster(WatchBody(cluster_id=cluster_id), db=db, uid=uid)
            project_id = watched["pipeline_id"]
            result = await get_pipeline_card(project_id, db=db, uid=uid)
            project = result["data"]
            return {
                "projectId": project_id,
                "project": {
                    "id": str(project["id"]),
                    "name": project["displayName"],
                    "stage": project["stage"],
                    "sourceClusterId": project["sourceClusterId"],
                    "analysisStatus": project["analysisStatus"],
                    "buildStatus": project["buildStatus"],
                },
            }
        except HTTPException as exc:
            await db.rollback()
            raise _tool_error(exc) from exc
        except Exception:
            await db.rollback()
            raise


@hosted_mcp_server.tool(
    description=(
        "Return paged, evidence-grounded analysis context and the exact output schema for "
        "the host model to analyze. This tool never calls an Immensity LLM. Retrieve every "
        "page before calling save_problem_analysis."
    ),
    annotations=READ_ONLY_CLOSED,
)
async def start_analysis(
    project_id: str,
    offset: int = 0,
    limit: int = ANALYSIS_PAGE_DEFAULT,
) -> dict[str, Any]:
    uid = _authenticated_subject()
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    page_size = max(1, min(int(limit), ANALYSIS_PAGE_MAX))
    async with AsyncSessionLocal() as db:
        try:
            card = await _owned_project(db, project_id, uid)
            all_items = await load_signal_source_items(db, card)
            usable_items = usable_signal_items(all_items)
            fingerprint = signal_source_fingerprint(all_items)
            page = usable_items[offset:offset + page_size]
            next_offset = offset + len(page)
            if next_offset >= len(usable_items):
                next_offset = None
            return {
                "projectId": project_id,
                "sourceFingerprint": fingerprint,
                "instructions": SIGNAL_ANALYSIS_INSTRUCTIONS,
                "analysisSchema": SignalProblemAnalysisOutput.model_json_schema(by_alias=True),
                "context": build_analysis_payload(
                    card,
                    page,
                    total_items=len(usable_items),
                ),
                "paging": {
                    "offset": offset,
                    "limit": page_size,
                    "returned": len(page),
                    "total": len(usable_items),
                    "nextOffset": next_offset,
                },
                "saveWith": "save_problem_analysis",
            }
        except HTTPException as exc:
            await db.rollback()
            raise _tool_error(exc) from exc
        except Exception:
            await db.rollback()
            raise


@hosted_mcp_server.tool(
    description=(
        "Validate and persist a host-model problem analysis into the signed-in user's "
        "Immensity project. Use the exact sourceFingerprint returned by start_analysis."
    ),
    annotations=WRITE_CLOSED,
)
async def save_problem_analysis(
    project_id: str,
    source_fingerprint: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    uid = _authenticated_subject()
    async with AsyncSessionLocal() as db:
        try:
            card = await _owned_project(db, project_id, uid)
            all_items = await load_signal_source_items(db, card)
            usable_items = usable_signal_items(all_items)
            current_fingerprint = signal_source_fingerprint(all_items)
            if source_fingerprint != current_fingerprint:
                raise ValueError(
                    "Project evidence changed during analysis. Call start_analysis again before saving."
                )

            try:
                output = SignalProblemAnalysisOutput.model_validate(analysis)
            except ValidationError as exc:
                raise ValueError("Analysis does not match the required Immensity schema") from exc

            now = utc_now()
            case = await find_signal_case(db, project_id, uid)
            if case is None:
                case = SignalAnalysisCase(
                    id=str(uuid.uuid4()),
                    pipeline_id=str(card.id),
                    user_id=uid,
                    status="queued",
                    created_at=now,
                    updated_at=now,
                )
                db.add(case)
                await db.flush()

            case.source_fingerprint = current_fingerprint
            case.source_updated_at = signal_source_updated_at(all_items)
            case.safe_error = None

            if not output.problems:
                case.status = "insufficient_evidence"
                case.progress_step = None
                case.progress_label = None
                case.analyzed_at = now
                case.updated_at = now
                await db.commit()
                return {
                    "projectId": project_id,
                    "analysis": {
                        "status": "insufficient_evidence",
                        "version": 0,
                        "problemCount": 0,
                    },
                }

            try:
                model_output = build_problem_model_output(output, usable_items)
                document = build_case_document(
                    card,
                    case,
                    all_items,
                    usable_items,
                    model_output,
                )
            except ValidationError as exc:
                raise ValueError(
                    "Analysis contains invalid or unknown evidence references"
                ) from exc

            next_version = int(
                await db.scalar(
                    select(func.coalesce(func.max(SignalAnalysisVersion.version), 0)).where(
                        SignalAnalysisVersion.case_id == case.id
                    )
                )
                or 0
            ) + 1
            payload = document.model_copy(update={"version": next_version}).model_dump(
                mode="json",
                by_alias=True,
            )
            version = SignalAnalysisVersion(
                id=str(uuid.uuid4()),
                case_id=case.id,
                version=next_version,
                schema_version=SIGNAL_ANALYSIS_SCHEMA_VERSION,
                provider="host_agent",
                model="user_selected",
                source_fingerprint=current_fingerprint,
                analysis=payload,
                generated_at=now,
            )
            db.add(version)
            await db.flush()
            case.current_version_id = version.id
            case.status = "ready"
            case.progress_step = None
            case.progress_label = None
            case.safe_error = None
            case.analyzed_at = now
            case.updated_at = now
            await db.commit()
            return {
                "projectId": project_id,
                "analysis": {
                    "status": "ready",
                    "version": next_version,
                    "problemCount": len(output.problems),
                },
            }
        except HTTPException as exc:
            await db.rollback()
            raise _tool_error(exc) from exc
        except Exception:
            await db.rollback()
            raise


@hosted_mcp_server.tool(
    description="List the Immensity projects available to the signed-in account.",
    annotations=READ_ONLY_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Select which Immensity project subsequent tools should use.",
    annotations=WRITE_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Return the analyzed Problem Case used as the source for Build design.",
    annotations=READ_ONLY_CLOSED,
)
async def get_problem_case() -> dict[str, Any]:
    return await _call(get_ide_problem_case)


@hosted_mcp_server.tool(
    description="Return the current Build workspace shown in Immensity.",
    annotations=READ_ONLY_CLOSED,
)
async def get_build_workspace() -> dict[str, Any]:
    return await _call(get_ide_build_workspace)


@hosted_mcp_server.tool(
    description="Generate a Build draft from the current analyzed Problem Case.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Replace one structured Build section with optimistic version checking.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Replace an editable Build markdown document.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Approve and freeze the current Build workspace version.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
async def approve_build_workspace(expected_version: int) -> dict[str, Any]:
    return await _call(
        approve_ide_build_workspace,
        ApproveSolutionRequest(expected_version=expected_version),
    )


@hosted_mcp_server.tool(
    description="Return the approved Build Contract. Call this before implementation.",
    annotations=READ_ONLY_CLOSED,
)
async def get_build_contract() -> dict[str, Any]:
    return await _call(get_ide_build_document, "build_contract")


@hosted_mcp_server.tool(
    description="List the focused documents in the approved Build package.",
    annotations=READ_ONLY_CLOSED,
)
async def list_build_documents() -> dict[str, Any]:
    return await _call(list_ide_build_documents)


@hosted_mcp_server.tool(
    description="Fetch one approved Build document by key.",
    annotations=READ_ONLY_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Return Build version, task counts, completion state, and blockers.",
    annotations=READ_ONLY_CLOSED,
)
async def get_project_status() -> dict[str, Any]:
    return await _call(get_ide_project_status)


@hosted_mcp_server.tool(
    description="Return the next todo task whose dependencies are complete.",
    annotations=READ_ONLY_CLOSED,
)
async def get_next_task() -> dict[str, Any]:
    return await _call(get_next_ide_task)


@hosted_mcp_server.tool(
    description="Move a task to todo or in progress and attach an optional note.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
async def update_task_progress(
    task_id: str,
    status: Literal["todo", "in_progress"],
    note: str | None = None,
) -> dict[str, Any]:
    return await _call(update_ide_task, task_id, TaskProgressRequest(status=status, note=note))


@hosted_mcp_server.tool(
    description="Mark a task blocked with the concrete reason.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
async def report_blocker(task_id: str, note: str) -> dict[str, Any]:
    return await _call(update_ide_task, task_id, TaskProgressRequest(status="blocked", note=note))


@hosted_mcp_server.tool(
    description="Mark a task done with the checks that actually passed.",
    annotations=DESTRUCTIVE_WRITE_CLOSED,
)
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


@hosted_mcp_server.tool(
    description="Compatibility view containing the analyzed problem and approved Build data.",
    annotations=READ_ONLY_CLOSED,
)
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
