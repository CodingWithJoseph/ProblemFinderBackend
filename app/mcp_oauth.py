"""OAuth authorization for hosted MCP clients."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import select, update

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import (
    McpOAuthClient,
    McpOAuthCode,
    McpOAuthPendingAuthorization,
    McpOAuthToken,
)

AUTHORIZATION_LIFETIME = timedelta(minutes=10)
ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
MCP_SCOPE = "mcp:use"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mint(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


class ImmensityOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Persist OAuth grants for hosted MCP clients."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with AsyncSessionLocal() as db:
            row = await db.get(McpOAuthClient, client_id)
            return OAuthClientInformationFull.model_validate(row.metadata_json) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with AsyncSessionLocal() as db:
            now = utc_now()
            row = await db.get(McpOAuthClient, client_info.client_id)
            metadata = client_info.model_dump(mode="json", exclude_none=True)
            if row:
                row.metadata_json = metadata
                row.updated_at = now
            else:
                db.add(McpOAuthClient(client_id=client_info.client_id, metadata_json=metadata))
            await db.commit()

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        pending_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(
                McpOAuthPendingAuthorization(
                    id=pending_id,
                    client_id=client.client_id,
                    redirect_uri=str(params.redirect_uri),
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    code_challenge=params.code_challenge,
                    scopes=params.scopes or [MCP_SCOPE],
                    state=params.state,
                    resource=params.resource,
                    expires_at=utc_now() + AUTHORIZATION_LIFETIME,
                )
            )
            await db.commit()
        return f"{self.settings.web_app_url.rstrip('/')}/mcp/authorize?request={pending_id}"

    async def describe_pending_authorization(self, request_id: str) -> dict | None:
        async with AsyncSessionLocal() as db:
            row = await db.get(McpOAuthPendingAuthorization, request_id)
            if row is None or row.consumed_at is not None or row.expires_at <= utc_now():
                return None
            client = await db.get(McpOAuthClient, row.client_id)
            metadata = client.metadata_json if client else {}
            return {
                "requestId": str(row.id),
                "clientName": metadata.get("client_name") or "your coding agent",
                "scopes": row.scopes,
                "expiresAt": row.expires_at,
            }

    async def complete_authorization(self, request_id: str, user_id: str) -> str:
        clear_code = _mint("imm_oauth_code")
        now = utc_now()
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(McpOAuthPendingAuthorization)
                    .where(McpOAuthPendingAuthorization.id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.consumed_at is not None or row.expires_at <= now:
                raise HTTPException(status_code=400, detail="This connection request has expired")
            row.consumed_at = now
            db.add(
                McpOAuthCode(
                    code_hash=hash_token(clear_code),
                    client_id=row.client_id,
                    user_id=user_id,
                    redirect_uri=row.redirect_uri,
                    redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                    code_challenge=row.code_challenge,
                    scopes=row.scopes,
                    resource=row.resource,
                    expires_at=now + AUTHORIZATION_LIFETIME,
                )
            )
            await db.commit()
            return construct_redirect_uri(row.redirect_uri, code=clear_code, state=row.state)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(McpOAuthCode).where(
                        McpOAuthCode.code_hash == hash_token(authorization_code),
                        McpOAuthCode.client_id == client.client_id,
                        McpOAuthCode.consumed_at == None,
                        McpOAuthCode.expires_at > utc_now(),
                    )
                )
            ).scalar_one_or_none()
            return self._authorization_code(row, authorization_code) if row else None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        now = utc_now()
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(McpOAuthCode)
                    .where(
                        McpOAuthCode.code_hash == hash_token(authorization_code.code),
                        McpOAuthCode.client_id == client.client_id,
                        McpOAuthCode.consumed_at == None,
                        McpOAuthCode.expires_at > now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise TokenError(error="invalid_grant", error_description="Authorization code is invalid or expired")
            row.consumed_at = now
            token = self._issue_token_pair(
                db,
                client_id=row.client_id,
                user_id=row.user_id,
                scopes=row.scopes,
                resource=row.resource,
            )
            await db.commit()
            return token

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        async with AsyncSessionLocal() as db:
            row = await self._valid_token_row(db, refresh_token, "refresh", client.client_id)
            if row is None:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=list(row.scopes),
                expires_at=_epoch(row.expires_at),
                subject=row.user_id,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = utc_now()
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(McpOAuthToken)
                    .where(
                        McpOAuthToken.token_hash == hash_token(refresh_token.token),
                        McpOAuthToken.token_type == "refresh",
                        McpOAuthToken.client_id == client.client_id,
                        McpOAuthToken.revoked_at == None,
                        McpOAuthToken.expires_at > now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise TokenError(error="invalid_grant", error_description="Refresh token is invalid or expired")
            requested_scopes = scopes or list(row.scopes)
            if not set(requested_scopes).issubset(set(row.scopes)):
                raise TokenError(error="invalid_scope", error_description="Requested scope was not originally granted")
            await db.execute(
                update(McpOAuthToken)
                .where(McpOAuthToken.family_id == row.family_id, McpOAuthToken.revoked_at == None)
                .values(revoked_at=now)
            )
            token = self._issue_token_pair(
                db,
                client_id=row.client_id,
                user_id=row.user_id,
                scopes=requested_scopes,
                resource=row.resource,
                selected_pipeline_id=row.selected_pipeline_id,
                family_id=row.family_id,
            )
            await db.commit()
            return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with AsyncSessionLocal() as db:
            row = await self._valid_token_row(db, token, "access")
            if row is None:
                return None
            row.last_used_at = utc_now()
            await db.commit()
            return AccessToken(
                token=token,
                client_id=row.client_id,
                scopes=list(row.scopes),
                expires_at=_epoch(row.expires_at),
                resource=row.resource or self.settings.mcp_public_url,
                subject=row.user_id,
                claims={
                    "token_id": str(row.id),
                    "selected_pipeline_id": str(row.selected_pipeline_id) if row.selected_pipeline_id else None,
                },
            )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        now = utc_now()
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(McpOAuthToken).where(McpOAuthToken.token_hash == hash_token(token.token))
                )
            ).scalar_one_or_none()
            if row:
                await db.execute(
                    update(McpOAuthToken)
                    .where(McpOAuthToken.family_id == row.family_id, McpOAuthToken.revoked_at == None)
                    .values(revoked_at=now)
                )
                await db.commit()

    async def select_project_for_token(self, token: str, user_id: str, pipeline_id: str) -> None:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(McpOAuthToken).where(
                        McpOAuthToken.token_hash == hash_token(token),
                        McpOAuthToken.token_type == "access",
                        McpOAuthToken.user_id == user_id,
                        McpOAuthToken.revoked_at == None,
                        McpOAuthToken.expires_at > utc_now(),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("This connection cannot switch projects")
            await db.execute(
                update(McpOAuthToken)
                .where(McpOAuthToken.family_id == row.family_id, McpOAuthToken.revoked_at == None)
                .values(selected_pipeline_id=pipeline_id)
            )
            await db.commit()

    async def _valid_token_row(self, db, token: str, token_type: str, client_id: str | None = None):
        filters = [
            McpOAuthToken.token_hash == hash_token(token),
            McpOAuthToken.token_type == token_type,
            McpOAuthToken.revoked_at == None,
            McpOAuthToken.expires_at > utc_now(),
        ]
        if client_id:
            filters.append(McpOAuthToken.client_id == client_id)
        return (await db.execute(select(McpOAuthToken).where(*filters))).scalar_one_or_none()

    @staticmethod
    def _authorization_code(row: McpOAuthCode, clear_code: str) -> AuthorizationCode:
        return AuthorizationCode(
            code=clear_code,
            scopes=list(row.scopes),
            expires_at=row.expires_at.timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            resource=row.resource,
            subject=row.user_id,
        )

    @staticmethod
    def _issue_token_pair(
        db,
        *,
        client_id: str,
        user_id: str,
        scopes: list[str],
        resource: str | None,
        selected_pipeline_id: str | None = None,
        family_id: str | None = None,
    ) -> OAuthToken:
        now = utc_now()
        access = _mint("imm_oauth_access")
        refresh = _mint("imm_oauth_refresh")
        family = family_id or str(uuid.uuid4())
        db.add_all(
            [
                McpOAuthToken(
                    token_hash=hash_token(access),
                    token_type="access",
                    token_prefix=access[:20],
                    client_id=client_id,
                    user_id=user_id,
                    scopes=scopes,
                    resource=resource,
                    selected_pipeline_id=selected_pipeline_id,
                    family_id=family,
                    expires_at=now + ACCESS_TOKEN_LIFETIME,
                ),
                McpOAuthToken(
                    token_hash=hash_token(refresh),
                    token_type="refresh",
                    token_prefix=refresh[:20],
                    client_id=client_id,
                    user_id=user_id,
                    scopes=scopes,
                    resource=resource,
                    selected_pipeline_id=selected_pipeline_id,
                    family_id=family,
                    expires_at=now + REFRESH_TOKEN_LIFETIME,
                ),
            ]
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


oauth_provider = ImmensityOAuthProvider()
