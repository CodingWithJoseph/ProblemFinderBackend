from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

import app.mcp_server as mcp_module
from app.mcp_server import hosted_mcp_app, hosted_mcp_server
from main import app as api_app


async def test_hosted_mcp_exposes_the_existing_build_toolset():
    tools = await hosted_mcp_server.list_tools()

    assert {tool.name for tool in tools} == {
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
        "list_projects",
        "select_project",
    }


async def test_hosted_mcp_starts_browser_auth_when_no_token_is_present():
    transport = ASGITransport(app=hosted_mcp_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert "resource_metadata=" in response.headers["www-authenticate"]


async def test_api_exposes_hosted_mcp_at_the_expected_path():
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


async def test_hosted_mcp_publishes_oauth_discovery_metadata():
    transport = ASGITransport(app=hosted_mcp_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        protected = await client.get("/.well-known/oauth-protected-resource/mcp")
        authorization = await client.get("/.well-known/oauth-authorization-server")

    assert protected.status_code == 200
    assert protected.json()["resource"].endswith("/mcp")
    assert authorization.status_code == 200
    assert authorization.json()["registration_endpoint"].endswith("/register")
    assert authorization.json()["code_challenge_methods_supported"] == ["S256"]


async def test_hosted_mcp_dynamically_registers_oauth_clients(monkeypatch):
    registered = []

    async def register(client):
        registered.append(client)

    monkeypatch.setattr(mcp_module.oauth_provider, "register_client", register)
    transport = ASGITransport(app=hosted_mcp_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/register",
            json={
                "client_name": "Codex",
                "redirect_uris": ["http://127.0.0.1:1455/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "mcp:use",
            },
        )

    assert response.status_code == 201
    assert response.json()["client_id"]
    assert response.json()["client_name"] == "Codex"
    assert len(registered) == 1


async def test_hosted_mcp_initializes_with_oauth_access_token(monkeypatch):
    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve_access(_db):
        return SimpleNamespace(id="oauth-token-1", pipeline_id="project-1", user_id="user-1")

    async def verify_token(token):
        return AccessToken(
            token=token,
            client_id="client-1",
            scopes=["mcp:use"],
            subject="user-1",
            claims={"token_id": "oauth-token-1", "selected_pipeline_id": "project-1"},
        )

    async def get_workspace(pipeline_id, _db, access):
        return {"projectId": pipeline_id, "userId": access.user_id}

    monkeypatch.setattr(mcp_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(mcp_module, "_access_for_call", resolve_access)
    monkeypatch.setattr(mcp_module.oauth_provider, "load_access_token", verify_token)
    monkeypatch.setattr(mcp_module, "get_ide_build_workspace", get_workspace)

    transport = ASGITransport(app=hosted_mcp_app)
    async with hosted_mcp_server.session_manager.run():
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer imm_oauth_access_test"},
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    response = await session.list_tools()
                    result = await session.call_tool("get_build_workspace", {})

    assert "get_build_workspace" in {tool.name for tool in response.tools}
    assert result.is_error is False
    assert result.structured_content == {"projectId": "project-1", "userId": "user-1"}
