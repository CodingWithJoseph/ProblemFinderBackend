from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
    }


async def test_hosted_mcp_rejects_requests_without_a_project_token():
    transport = ASGITransport(app=hosted_mcp_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing project access token"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_api_exposes_hosted_mcp_at_the_expected_path():
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing project access token"


async def test_hosted_mcp_initializes_with_a_project_token(monkeypatch):
    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve_token(_token, _db):
        return SimpleNamespace(id="token-1", pipeline_id="project-1", user_id="user-1")

    async def get_workspace(pipeline_id, _db, access):
        return {"projectId": pipeline_id, "userId": access.user_id}

    monkeypatch.setattr(mcp_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(mcp_module, "resolve_ide_access_token", resolve_token)
    monkeypatch.setattr(mcp_module, "get_ide_build_workspace", get_workspace)

    transport = ASGITransport(app=hosted_mcp_app)
    async with hosted_mcp_server.session_manager.run():
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer imm_mcp_test"},
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
