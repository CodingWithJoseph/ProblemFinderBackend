"""Smoke-test the deployed public Immensity MCP endpoint.

Unauthenticated checks validate health, readiness, OAuth discovery, and the MCP
authentication challenge. Supplying an OAuth access token also opens a real MCP
session, verifies the complete tool surface, lists projects, and optionally
exercises the read flow for one approved project.
"""

from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlparse

from httpx import AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


DEFAULT_MCP_URL = "https://mcp.useimmensity.com/mcp"
REQUIRED_OAUTH_SCOPES = {"mcp:use", "offline_access"}

REQUIRED_TOOLS = {
    "list_projects",
    "select_project",
    "get_problem_case",
    "get_build_workspace",
    "generate_build_workspace",
    "update_build_section",
    "update_build_document",
    "approve_build_workspace",
    "get_build_contract",
    "list_build_documents",
    "get_build_document",
    "get_project_status",
    "get_next_task",
    "update_task_progress",
    "report_blocker",
    "submit_verification",
    "get_project_context",
}

READ_FLOW = (
    "get_problem_case",
    "get_build_workspace",
    "get_build_contract",
    "get_project_status",
    "get_next_task",
)


def origin_for(mcp_url: str) -> str:
    parsed = urlparse(mcp_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--mcp-url must be an absolute HTTP(S) URL")
    return f"{parsed.scheme}://{parsed.netloc}"


async def check_public_surface(mcp_url: str, timeout: float) -> None:
    origin = origin_for(mcp_url)
    async with AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for path, expected in (("/health", "ok"), ("/ready", "ready")):
            response = await client.get(f"{origin}{path}")
            payload = response.json()
            if response.status_code != 200 or payload.get("status") != expected:
                raise RuntimeError(f"{path} failed: HTTP {response.status_code} {payload}")
            print(f"PASS {path}")

        authorization = await client.get(f"{origin}/.well-known/oauth-authorization-server")
        if authorization.status_code != 200:
            raise RuntimeError(f"OAuth authorization metadata failed: HTTP {authorization.status_code}")
        authorization_payload = authorization.json()
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            if not authorization_payload.get(key):
                raise RuntimeError(f"OAuth authorization metadata is missing {key}")
        scopes_supported = set(authorization_payload.get("scopes_supported") or [])
        missing_scopes = sorted(REQUIRED_OAUTH_SCOPES - scopes_supported)
        if missing_scopes:
            raise RuntimeError(
                f"OAuth authorization metadata is missing scopes: {', '.join(missing_scopes)}"
            )
        print("PASS OAuth authorization metadata")

        protected = await client.get(f"{origin}/.well-known/oauth-protected-resource/mcp")
        if protected.status_code != 200:
            raise RuntimeError(f"OAuth protected-resource metadata failed: HTTP {protected.status_code}")
        if protected.json().get("resource") != mcp_url.rstrip("/"):
            raise RuntimeError(
                "OAuth protected-resource metadata does not advertise the requested MCP URL"
            )
        print("PASS OAuth protected-resource metadata")

        challenge = await client.post(mcp_url, json={})
        if challenge.status_code != 401:
            raise RuntimeError(f"Unauthenticated MCP request should be 401, got {challenge.status_code}")
        if "resource_metadata=" not in challenge.headers.get("www-authenticate", ""):
            raise RuntimeError("MCP authentication challenge is missing resource metadata")
        print("PASS unauthenticated MCP challenge")


def successful_payload(result, tool_name: str) -> dict:
    if result.is_error:
        details = " ".join(getattr(item, "text", "") for item in result.content)
        raise RuntimeError(f"{tool_name} failed: {details or 'tool returned an error'}")
    return result.structured_content or {}


async def check_authenticated_surface(
    mcp_url: str,
    access_token: str,
    project_id: str | None,
    timeout: float,
) -> None:
    async with AsyncClient(
        timeout=timeout,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                missing = sorted(REQUIRED_TOOLS - names)
                if missing:
                    raise RuntimeError(f"MCP is missing required tools: {', '.join(missing)}")
                print(f"PASS tool surface ({len(REQUIRED_TOOLS)} required tools)")

                projects_result = await session.call_tool("list_projects", {})
                projects_payload = successful_payload(projects_result, "list_projects")
                projects = projects_payload.get("projects", [])
                print(f"PASS list_projects ({len(projects)} projects)")

                if project_id is None:
                    return

                project_ids = {str(project.get("id")) for project in projects}
                if project_id not in project_ids:
                    raise RuntimeError(f"Project {project_id} was not returned by list_projects")

                selected = await session.call_tool("select_project", {"project_id": project_id})
                successful_payload(selected, "select_project")
                print(f"PASS select_project ({project_id})")

                for tool_name in READ_FLOW:
                    result = await session.call_tool(tool_name, {})
                    successful_payload(result, tool_name)
                    print(f"PASS {tool_name}")

                for tool_name in (
                    "update_task_progress",
                    "report_blocker",
                    "submit_verification",
                ):
                    if tool_name not in names:
                        raise RuntimeError(f"Missing write tool {tool_name}")
                print("PASS write-tool availability (not invoked)")


async def run(args) -> int:
    mcp_url = args.mcp_url.rstrip("/")
    await check_public_surface(mcp_url, args.timeout)

    if args.access_token:
        await check_authenticated_surface(
            mcp_url,
            args.access_token,
            args.project_id,
            args.timeout,
        )
    elif args.project_id:
        raise RuntimeError("--project-id requires --access-token")
    else:
        print("SKIP authenticated MCP checks (no --access-token supplied)")

    print("Public MCP smoke checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help="Public Streamable HTTP MCP resource URL",
    )
    parser.add_argument(
        "--access-token",
        help="OAuth access token for authenticated tool checks",
    )
    parser.add_argument(
        "--project-id",
        help="Approved Immensity project to use for the read-flow checks",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
