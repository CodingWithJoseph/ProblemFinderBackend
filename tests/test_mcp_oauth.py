from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import app.mcp_oauth as oauth_module
from app.mcp_oauth import AUTHORIZATION_LIFETIME, ImmensityOAuthProvider, hash_token, utc_now
from app.models import McpOAuthPendingAuthorization


class Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class Session:
    def __init__(self, execute_row=None):
        self.execute_row = execute_row
        self.added = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    async def execute(self, _statement):
        return Result(self.execute_row)

    async def commit(self):
        self.commits += 1


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def client_info():
    return OAuthClientInformationFull(
        client_id="client-1",
        client_name="Codex",
        redirect_uris=[AnyUrl("http://127.0.0.1:1455/callback")],
        token_endpoint_auth_method="none",
    )


async def test_authorize_sends_the_client_to_immensity_without_issuing_a_token(monkeypatch):
    session = Session()
    monkeypatch.setattr(oauth_module, "AsyncSessionLocal", lambda: SessionContext(session))
    provider = ImmensityOAuthProvider()

    redirect = await provider.authorize(
        client_info(),
        AuthorizationParams(
            state="client-state",
            scopes=["mcp:use"],
            code_challenge="pkce-challenge",
            redirect_uri=AnyUrl("http://127.0.0.1:1455/callback"),
            redirect_uri_provided_explicitly=True,
            resource=provider.settings.mcp_public_url,
        ),
    )

    assert redirect.startswith(f"{provider.settings.web_app_url}/mcp/authorize?request=")
    assert len(session.added) == 1
    pending = session.added[0]
    assert pending.client_id == "client-1"
    assert pending.code_challenge == "pkce-challenge"
    assert pending.expires_at <= utc_now() + AUTHORIZATION_LIFETIME
    assert session.commits == 1


async def test_browser_approval_returns_a_one_time_hashed_authorization_code(monkeypatch):
    pending = McpOAuthPendingAuthorization(
        id="3cab50ac-21bf-4a50-8048-4a3805fd4278",
        client_id="client-1",
        redirect_uri="http://127.0.0.1:1455/callback",
        redirect_uri_provided_explicitly=True,
        code_challenge="pkce-challenge",
        scopes=["mcp:use"],
        state="client-state",
        resource="http://127.0.0.1:8000/mcp",
        expires_at=utc_now() + timedelta(minutes=5),
    )
    session = Session(execute_row=pending)
    monkeypatch.setattr(oauth_module, "AsyncSessionLocal", lambda: SessionContext(session))
    provider = ImmensityOAuthProvider()

    redirect = await provider.complete_authorization(str(pending.id), "user-1")

    query = parse_qs(urlparse(redirect).query)
    clear_code = query["code"][0]
    assert query["state"] == ["client-state"]
    assert pending.consumed_at is not None
    code_row = session.added[0]
    assert code_row.user_id == "user-1"
    assert code_row.code_hash == hash_token(clear_code)
    assert clear_code not in code_row.code_hash
    assert session.commits == 1
