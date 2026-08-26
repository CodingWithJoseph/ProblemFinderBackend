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


async def test_userinfo_returns_verified_firebase_email(monkeypatch):
    async def load_access_token(_token):
        return SimpleNamespace(
            subject="firebase-user-1",
            scopes=["mcp:use", "openid", "email"],
        )

    monkeypatch.setattr(openai_auth_module.oauth_provider, "load_access_token", load_access_token)
    monkeypatch.setattr(
        openai_auth_module.firebase_auth,
        "get_user",
        lambda _uid: SimpleNamespace(email="dev@example.com", email_verified=True),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer imm_oauth_access_test"},
    ) as client:
        response = await client.get("/userinfo")

    assert response.status_code == 200
    assert response.json() == {
        "sub": "firebase-user-1",
        "email": "dev@example.com",
        "email_verified": True,
    }


async def test_userinfo_requires_openid_and_email_scopes(monkeypatch):
    async def load_access_token(_token):
        return SimpleNamespace(subject="firebase-user-1", scopes=["mcp:use"])

    monkeypatch.setattr(openai_auth_module.oauth_provider, "load_access_token", load_access_token)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer imm_oauth_access_test"},
    ) as client:
        response = await client.get("/userinfo")

    assert response.status_code == 403
