"""Supabase user JWT verification. Ownership uses the verified Supabase UUID."""

import asyncio
import time
import uuid

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import AccountProfile

settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)


def unauthorized(detail: str = "Invalid or expired access token") -> HTTPException:
    return HTTPException(401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


class SupabaseTokenVerifier:
    def __init__(self):
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._last_fetch = 0.0
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> jwt.PyJWK:
        now = time.monotonic()
        if now < self._expires_at and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now < self._expires_at and now - self._last_fetch < 10:
                raise unauthorized()
            try:
                async with httpx.AsyncClient(timeout=settings.supabase_auth_timeout_seconds) as client:
                    response = await client.get(f"{settings.supabase_issuer}/.well-known/jwks.json")
                    response.raise_for_status()
                    keys = {
                        key["kid"]: jwt.PyJWK.from_dict(key)
                        for key in response.json()["keys"]
                        if key.get("alg") in {"ES256", "RS256"} and key.get("use", "sig") == "sig"
                    }
            except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWTError):
                raise HTTPException(503, detail="Authentication service is unavailable") from None
            self._keys = keys
            self._last_fetch = now
            self._expires_at = now + settings.supabase_jwks_cache_seconds
            if kid not in keys:
                raise unauthorized()
            return keys[kid]

    async def verify(self, token: str) -> dict:
        if len(token) > 16_384:
            raise unauthorized()
        try:
            header = jwt.get_unverified_header(token)
            alg = header.get("alg")
            kid = header.get("kid")
            if alg not in {"ES256", "RS256"} or not isinstance(kid, str) or not kid:
                raise unauthorized()
            key = await self._key(kid)
            if key.algorithm_name != alg:
                raise unauthorized()
            claims = jwt.decode(
                token, key.key, algorithms=[alg],
                issuer=settings.supabase_issuer, audience=settings.supabase_jwt_audience,
                leeway=10, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            subject = claims["sub"]
            if str(uuid.UUID(subject)) != subject:
                raise unauthorized()
            if claims.get("role") != "authenticated" or claims.get("is_anonymous") is True:
                raise unauthorized()
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
            raise unauthorized() from None


token_verifier = SupabaseTokenVerifier()


async def lock_active_account(db: AsyncSession, uid: str) -> None:
    profile = (await db.execute(
        select(AccountProfile).where(AccountProfile.id == uid).with_for_update()
    )).scalar_one_or_none()
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(403, detail="Account is unavailable")


async def get_claims(
    request: Request,
    token: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    if token is None:
        raise unauthorized("Missing authentication credentials")
    claims = await token_verifier.verify(token.credentials)
    request.state.uid = claims["sub"]
    request.state.auth_claims = claims
    request.state.access_token = token.credentials
    return claims


async def get_uid(
    request: Request,
    claims: dict = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> str:
    uid = claims["sub"]
    query = select(AccountProfile).where(AccountProfile.id == uid).with_for_update(read=request.method in {"GET", "HEAD"})
    profile = (await db.execute(query)).scalar_one_or_none()
    if profile is None:
        metadata = claims.get("user_metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        name = metadata.get("display_name")
        if not isinstance(name, str):
            name = " ".join(str(metadata.get(key) or "") for key in ("first_name", "last_name"))
        await db.execute(insert(AccountProfile).values(
            id=uid, display_name=name.strip()[:160], focus_areas=[], onboarding_complete=False,
        ).on_conflict_do_nothing(index_elements=[AccountProfile.id]))
        profile = (await db.execute(query)).scalar_one()
    # Hold the row lock until the request commits, so deletion cannot race new writes.
    if profile is not None and profile.deleted_at is not None:
        raise HTTPException(403, detail="Account has been deleted")
    return uid


async def supabase_user(access_token: str) -> dict:
    if not settings.supabase_publishable_key:
        raise HTTPException(503, detail="Authentication service is not configured")
    try:
        async with httpx.AsyncClient(timeout=settings.supabase_auth_timeout_seconds) as client:
            response = await client.get(
                f"{settings.supabase_issuer}/user",
                headers={"apikey": settings.supabase_publishable_key, "Authorization": f"Bearer {access_token}"},
            )
        if response.status_code in {401, 403, 404}:
            raise unauthorized()
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail="Authentication service is unavailable") from None


async def supabase_admin_user(uid: str, *, delete: bool = False) -> dict:
    if not settings.supabase_secret_key:
        raise HTTPException(503, detail="Account administration is not configured")
    try:
        async with httpx.AsyncClient(timeout=settings.supabase_auth_timeout_seconds) as client:
            response = await client.request(
                "DELETE" if delete else "GET", f"{settings.supabase_issuer}/admin/users/{uid}",
                headers={"apikey": settings.supabase_secret_key, "Authorization": f"Bearer {settings.supabase_secret_key}"},
            )
        if delete and response.status_code == 404:
            return {}
        if response.status_code == 404:
            raise unauthorized()
        response.raise_for_status()
        return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail="Account administration is unavailable") from None


async def require_recent_user(request: Request, claims: dict) -> dict:
    # Refresh changes iat, not authentication time. Require a recent AMR event.
    now = time.time()
    recent = any(
        isinstance(entry, dict) and entry.get("method") in {"password", "otp", "oauth", "totp", "mfa/totp"}
        and isinstance(entry.get("timestamp"), (int, float))
        and now - 300 <= entry["timestamp"] <= now + 10
        for entry in claims.get("amr", [])
    )
    if not recent:
        raise HTTPException(403, detail="Please sign in again before deleting your account")
    user = await supabase_user(request.state.access_token)
    if user.get("id") != claims["sub"]:
        raise unauthorized()
    return user
