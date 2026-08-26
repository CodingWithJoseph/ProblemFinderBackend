from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
import pytest

import app.mcp_server as mcp_module
from app.models import Pipeline, SignalAnalysisCase, SignalAnalysisJob, SignalAnalysisVersion
from app.mcp_server import hosted_mcp_app, hosted_mcp_server
from conftest import FakeResult, make_cluster, make_cluster_item, make_pipeline
from main import app as api_app


async def test_hosted_mcp_exposes_the_existing_build_toolset():
    tools = await hosted_mcp_server.list_tools()

    assert len(tools) == 22
    assert {tool.name for tool in tools} == {
        "search_problems",
        "create_project_from_problem",
        "start_analysis",
        "save_problem_analysis",
        "get_problem_case",
        "get_build_workspace",
        "generate_build_workspace",
        "save_build_workspace",
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


class SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return None


def use_oauth_user(monkeypatch, fake_db, subject="oauth-user"):
    monkeypatch.setattr(mcp_module, "AsyncSessionLocal", lambda: SessionContext(fake_db))
    monkeypatch.setattr(
        mcp_module,
        "get_access_token",
        lambda: SimpleNamespace(subject=subject, token="oauth-token", claims={}),
    )


async def test_search_problems_reuses_cluster_search_and_returns_concise_candidates(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    cluster = make_cluster()
    item = make_cluster_item(cluster_id=cluster.id)
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[item]),
            FakeResult(rows=[]),
        ],
    )

    result = await mcp_module.search_problems("late invoices")

    assert result == {
        "problems": [{
            "clusterId": "42",
            "name": "Freelance invoicing pain",
            "problemStatement": "Freelancers waste hours every week chasing unpaid invoices.",
            "summary": "Freelancers struggle to get invoices paid on time.",
            "evidenceCount": 1,
            "sources": ["reddit"],
            "communities": ["r/freelance"],
            "hasProject": False,
        }],
        "returned": 1,
        "total": 1,
    }


async def test_create_project_from_problem_uses_oauth_user_and_watch_workflow(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    cluster = make_cluster()
    item = make_cluster_item(cluster_id=cluster.id)
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),
            FakeResult(rows=[cluster]),
            FakeResult(rows=[(item.id,)]),
        ],
    )

    async def load_created_project(project_id, db, uid):
        card = next(row for row in db.added if isinstance(row, Pipeline))
        assert project_id == card.id
        assert uid == "oauth-user"
        return {
            "data": {
                "id": card.id,
                "displayName": card.name,
                "stage": card.stage,
                "sourceClusterId": card.source_cluster_id,
                "analysisStatus": "not_started",
                "buildStatus": "not_started",
            }
        }

    monkeypatch.setattr(mcp_module, "get_pipeline_card", load_created_project)

    result = await mcp_module.create_project_from_problem("42")

    project = next(row for row in fake_db.added if isinstance(row, Pipeline))
    assert project.user_id == "oauth-user"
    assert project.source_cluster_id == "42"
    assert result["projectId"] == project.id
    assert result["project"] == {
        "id": project.id,
        "name": cluster.name,
        "stage": "watching",
        "sourceClusterId": "42",
        "analysisStatus": "not_started",
        "buildStatus": "not_started",
    }


async def test_create_project_from_problem_returns_an_existing_owned_project(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    card = make_pipeline(
        user_id="oauth-user",
        name="Late invoices",
        source_cluster_id="42",
    )
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        FakeResult(rows=[card]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
    ])

    result = await mcp_module.create_project_from_problem("42")

    assert result["projectId"] == card.id
    assert result["project"]["name"] == "Late invoices"
    assert result["project"]["sourceClusterId"] == "42"
    assert not any(isinstance(row, Pipeline) for row in fake_db.added)


async def test_start_analysis_returns_host_model_context_without_queueing_backend_job(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    card = make_pipeline(user_id="oauth-user")
    item = make_cluster_item(cluster_id=42)
    fake_db.stub(execute=[FakeResult(rows=[card])])

    async def load_items(_db, _card):
        return [item]

    monkeypatch.setattr(mcp_module, "load_signal_source_items", load_items)

    result = await mcp_module.start_analysis(card.id)

    assert result["projectId"] == card.id
    assert result["sourceFingerprint"].startswith("sha256:")
    assert result["context"]["evidence"][0]["id"] == str(item.id)
    assert result["paging"] == {
        "offset": 0,
        "limit": 50,
        "returned": 1,
        "total": 1,
        "nextOffset": None,
    }
    assert result["saveWith"] == "save_problem_analysis"
    assert "problems" in result["analysisSchema"]["properties"]
    assert not any(isinstance(row, SignalAnalysisCase) for row in fake_db.added)
    assert not any(isinstance(row, SignalAnalysisJob) for row in fake_db.added)


async def test_start_analysis_rejects_a_project_not_owned_by_oauth_user(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    fake_db.stub(execute=[FakeResult(rows=[])])

    with pytest.raises(ValueError, match="Pipeline card not found"):
        await mcp_module.start_analysis("another-users-project")

    assert not any(isinstance(row, SignalAnalysisCase) for row in fake_db.added)
    assert not any(isinstance(row, SignalAnalysisJob) for row in fake_db.added)


async def test_save_problem_analysis_persists_host_model_result_as_current_project_analysis(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    card = make_pipeline(user_id="oauth-user", source_cluster_id="42")
    item = make_cluster_item(cluster_id=42)
    fake_db.stub(
        execute=[FakeResult(rows=[card]), FakeResult(rows=[])],
        scalar=[0],
    )

    async def load_items(_db, _card):
        return [item]

    monkeypatch.setattr(mcp_module, "load_signal_source_items", load_items)
    fingerprint = mcp_module.signal_source_fingerprint([item])
    analysis = {
        "problems": [{
            "id": "problem-1",
            "title": "Invoice follow-up is repetitive",
            "description": "Freelancers repeatedly chase late invoices.",
            "affectedUsers": ["freelancers"],
            "situation": "After an invoice passes its due date",
            "evidenceIds": [str(item.id)],
            "relatedProblemIds": [],
        }]
    }

    result = await mcp_module.save_problem_analysis(card.id, fingerprint, analysis)

    case = next(row for row in fake_db.added if isinstance(row, SignalAnalysisCase))
    version = next(row for row in fake_db.added if isinstance(row, SignalAnalysisVersion))
    assert case.pipeline_id == card.id
    assert case.user_id == "oauth-user"
    assert case.status == "ready"
    assert case.current_version_id == version.id
    assert version.provider == "host_agent"
    assert version.model == "user_selected"
    assert version.analysis["problemUnits"][0]["evidenceIds"] == [str(item.id)]
    assert result == {
        "projectId": card.id,
        "analysis": {
            "status": "ready",
            "version": 1,
            "problemCount": 1,
        },
    }
    assert not any(isinstance(row, SignalAnalysisJob) for row in fake_db.added)


async def test_save_problem_analysis_rejects_stale_analysis_context(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    card = make_pipeline(user_id="oauth-user", source_cluster_id="42")
    item = make_cluster_item(cluster_id=42)
    fake_db.stub(execute=[FakeResult(rows=[card])])

    async def load_items(_db, _card):
        return [item]

    monkeypatch.setattr(mcp_module, "load_signal_source_items", load_items)

    with pytest.raises(ValueError, match="evidence changed"):
        await mcp_module.save_problem_analysis(card.id, "sha256:stale", {"problems": []})

    assert not any(isinstance(row, SignalAnalysisCase) for row in fake_db.added)


async def test_generate_build_workspace_returns_context_for_the_host_model(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    card = make_pipeline(user_id="oauth-user")

    async def resolve_access(_db):
        return SimpleNamespace(pipeline_id=card.id, user_id="oauth-user")

    async def owned_project(_db, project_id, uid):
        assert project_id == card.id
        assert uid == "oauth-user"
        return card

    async def prepare(_db, prepared_card, uid, request):
        assert prepared_card is card
        assert uid == "oauth-user"
        assert request.delivery_targets == ["web"]
        return {
            "projectId": card.id,
            "problemCaseVersion": 3,
            "instructions": "design with the host model",
            "buildSchema": {"type": "object"},
            "context": {"request": {}, "problemCase": {}},
            "saveWith": "save_build_workspace",
        }

    monkeypatch.setattr(mcp_module, "_access_for_call", resolve_access)
    monkeypatch.setattr(mcp_module, "_owned_project", owned_project)
    monkeypatch.setattr(mcp_module, "prepare_host_build_context", prepare)

    result = await mcp_module.generate_build_workspace()

    assert result["problemCaseVersion"] == 3
    assert result["saveWith"] == "save_build_workspace"


async def test_save_build_workspace_persists_the_host_model_result(
    monkeypatch,
    fake_db,
):
    use_oauth_user(monkeypatch, fake_db)
    card = make_pipeline(user_id="oauth-user")

    async def resolve_access(_db):
        return SimpleNamespace(pipeline_id=card.id, user_id="oauth-user")

    async def owned_project(_db, project_id, uid):
        return card

    async def save(_db, saved_card, uid, *, problem_case_version, request, build):
        assert saved_card is card
        assert uid == "oauth-user"
        assert problem_case_version == 3
        assert request.delivery_targets == ["web"]
        assert build == {"mvpGoal": "Ship it"}
        return {
            "projectId": card.id,
            "build": {"status": "ready", "version": 1, "problemCaseVersion": 3},
        }

    monkeypatch.setattr(mcp_module, "_access_for_call", resolve_access)
    monkeypatch.setattr(mcp_module, "_owned_project", owned_project)
    monkeypatch.setattr(mcp_module, "save_host_build_workspace", save)

    result = await mcp_module.save_build_workspace(3, {"mvpGoal": "Ship it"})

    assert result["build"]["status"] == "ready"
    assert result["build"]["problemCaseVersion"] == 3


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
    assert {"mcp:use", "offline_access", "openid", "email"}.issubset(
        set(authorization.json()["scopes_supported"])
    )


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
                "scope": "mcp:use offline_access openid email",
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
