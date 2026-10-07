from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

import app.routes.openai_plugin_auth as openai_auth_module
from main import app


async def test_openai_domain_challenge_returns_only_configured_token(monkeypatch):
    monkeypatch.setattr(openai_auth_module.settings, "openai_apps_challenge_token", "verify-123")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/.well-known/openai-apps-challenge")

    assert response.status_code == 200
    assert response.text == "verify-123"


async def test_openai_domain_challenge_is_unavailable_until_token_is_configured(monkeypatch):
    monkeypatch.setattr(openai_auth_module.settings, "openai_apps_challenge_token", "")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/.well-known/openai-apps-challenge")

    assert response.status_code == 404
    assert response.text == ""


async def test_openid_configuration_advertises_email_userinfo_support():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/.well-known/openid-configuration")

    payload = response.json()
    assert response.status_code == 200
    assert payload["userinfo_endpoint"].endswith("/userinfo")
    assert {"openid", "email"}.issubset(set(payload["scopes_supported"]))
    assert payload["subject_types_supported"] == ["public"]
    assert payload["jwks_uri"].endswith("/.well-known/jwks.json")


async def test_openid_jwks_endpoint_is_available():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.json() == {"keys": []}


async def test_userinfo_returns_verified_supabase_email(monkeypatch):
    async def load_access_token(_token):
        return SimpleNamespace(
            subject="11111111-1111-4111-8111-111111111111",
            scopes=["mcp:use", "openid", "email"],
        )

    monkeypatch.setattr(openai_auth_module.oauth_provider, "load_access_token", load_access_token)
    async def admin_user(_uid):
        return {"email": "dev@example.com", "email_confirmed_at": "2026-10-06T00:00:00Z"}
    monkeypatch.setattr(openai_auth_module, "supabase_admin_user", admin_user)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer imm_oauth_access_test"},
    ) as client:
        response = await client.get("/userinfo")

    assert response.status_code == 200
    assert response.json() == {
        "sub": "11111111-1111-4111-8111-111111111111",
        "email": "dev@example.com",
        "email_verified": True,
    }


async def test_userinfo_requires_openid_and_email_scopes(monkeypatch):
    async def load_access_token(_token):
        return SimpleNamespace(subject="11111111-1111-4111-8111-111111111111", scopes=["mcp:use"])

    monkeypatch.setattr(openai_auth_module.oauth_provider, "load_access_token", load_access_token)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer imm_oauth_access_test"},
    ) as client:
        response = await client.get("/userinfo")

    assert response.status_code == 403
