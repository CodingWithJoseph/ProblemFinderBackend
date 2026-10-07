"""Opt-in real PostgreSQL tests; TEST_DATABASE_URL must be an isolated local DB."""

import os
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, Depends, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.account_deletion as deletion
import app.routes.accounts as accounts
from app.auth import get_claims, get_uid, settings
from app.db import Base, get_db
from app.models import AccountProfile, Pipeline, Problem, SearchSession, McpOAuthClient, McpOAuthToken
from app.mcp_oauth import ImmensityOAuthProvider, hash_token, utc_now

UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="Set TEST_DATABASE_URL to isolated local PostgreSQL")


@pytest_asyncio.fixture
async def postgres_app(monkeypatch):
    url = urlparse(DSN.replace("postgresql+asyncpg", "postgresql"))
    assert url.hostname in {"127.0.0.1", "localhost"} and url.path == "/auth_migration", "Refusing non-test database"
    schema = f"auth_test_{uuid.uuid4().hex}"
    admin = create_async_engine(DSN)
    async with admin.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(DSN, connect_args={"server_settings": {"search_path": f"{schema},public"}})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Run the actual new migration twice, proving its idempotent schema contract.
        raw = await connection.get_raw_connection()
        sql = (Path(__file__).parents[1] / "migrations/0055_supabase_accounts.sql").read_text()
        await raw.driver_connection.execute(sql)
        await raw.driver_connection.execute(sql)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI(); app.include_router(accounts.router)
    async def db_override():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    async def claims_override(request: Request):
        uid = request.headers.get("x-test-user", UID)
        claims = {"sub": uid, "user_metadata": {"first_name": "Test", "last_name": "Person"}}
        request.state.auth_claims = claims
        request.state.access_token = "synthetic-token"
        return claims
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_claims] = claims_override
    @app.get("/protected")
    async def protected(uid: str = Depends(get_uid)):
        return {"id": uid}
    async def recent(_request, _claims):
        return {"id": UID}
    monkeypatch.setattr(accounts, "require_recent_user", recent)
    monkeypatch.setattr(settings, "supabase_secret_key", "synthetic-admin-key")
    async def admin_delete(_uid, *, delete=False):
        return {}
    monkeypatch.setattr(deletion, "supabase_admin_user", admin_delete)
    monkeypatch.setattr("app.mcp_oauth.AsyncSessionLocal", sessions)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, sessions
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_profile_bootstrap_onboarding_and_account_isolation(postgres_app):
    client, sessions = postgres_app
    assert (await client.get("/account")).json()["displayName"] == "Test Person"
    assert (await client.get("/onboarding/status")).json() == {"complete": False}
    assert (await client.post("/onboarding", json={"focusAreas": [" health ", "health"]})).status_code == 200
    profile = (await client.get("/account")).json()
    assert profile["onboardingComplete"] is True and profile["focusAreas"] == ["health"]
    await client.patch("/account", json={"displayName": "My name"})
    assert (await client.get("/account")).json()["displayName"] == "My name"
    assert (await client.get("/account", headers={"x-test-user": OTHER})).json()["displayName"] == "Test Person"
    async with sessions() as db:
        assert len((await db.execute(select(AccountProfile))).scalars().all()) == 2


async def test_waitlist_deduplicates_in_postgresql(postgres_app):
    client, _ = postgres_app
    first = await client.post("/waitlist", json={"email": " Builder@Example.com ", "source": "Hero"})
    second = await client.post("/waitlist", json={"email": "builder@example.com", "source": "Footer"})
    assert first.json() == {"success": True, "alreadySubscribed": False}
    assert second.json() == {"success": True, "alreadySubscribed": True}
    assert (await client.post("/waitlist", json={"email": "invalid"})).status_code == 422


async def test_delete_cleans_owned_records_and_revokes_mcp(postgres_app):
    client, sessions = postgres_app
    await client.get("/account")
    async with sessions() as db:
        db.add_all([
            Pipeline(user_id=UID, name="Delete this"), Pipeline(user_id=OTHER, name="Keep this"),
            Problem(user_id=UID, title="Orphan problem"), SearchSession(user_id=UID),
            McpOAuthClient(client_id="test-client", metadata_json={}),
        ]); await db.commit()
        db.add(McpOAuthToken(token_hash=hash_token("synthetic-grant"), token_type="access", token_prefix="synthetic",
            client_id="test-client", user_id=UID, scopes=["mcp:use"], family_id=str(uuid.uuid4()),
            expires_at=utc_now() + timedelta(hours=1)))
        await db.commit()
    assert (await client.delete("/account")).json() == {"success": True}
    assert (await client.delete("/account")).json() == {"success": True}
    assert (await client.get("/protected")).status_code == 403
    assert (await client.post("/onboarding", json={})).status_code == 403
    async with sessions() as db:
        assert (await db.execute(select(Problem).where(Problem.user_id == UID))).scalars().all() == []
        assert (await db.execute(select(SearchSession).where(SearchSession.user_id == UID))).scalars().all() == []
        rows = (await db.execute(select(Pipeline))).scalars().all()
        assert len(rows) == 1 and rows[0].user_id == OTHER
        token = (await db.execute(select(McpOAuthToken))).scalar_one()
        assert token.revoked_at is not None
    assert await ImmensityOAuthProvider().load_access_token("synthetic-grant") is None


async def test_provider_failure_keeps_deny_fence_and_retry_finishes(postgres_app, monkeypatch):
    client, sessions = postgres_app
    await client.get("/account")
    async def unavailable(_uid, *, delete=False):
        raise HTTPException(503, detail="Synthetic provider outage")
    monkeypatch.setattr(deletion, "supabase_admin_user", unavailable)
    assert (await client.delete("/account")).status_code == 503
    assert (await client.get("/protected")).status_code == 403
    async with sessions() as db:
        profile = (await db.execute(select(AccountProfile).where(AccountProfile.id == UID))).scalar_one()
        assert profile.deleted_at is not None and profile.deletion_completed_at is None
    async def available(_uid, *, delete=False):
        return {}
    monkeypatch.setattr(deletion, "supabase_admin_user", available)
    async with sessions() as db:
        await deletion.finish_account_deletion(db, UID)
    assert (await client.delete("/account")).json() == {"success": True}
