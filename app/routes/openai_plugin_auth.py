"""OpenAI plugin verification and OIDC UserInfo endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.auth import supabase_admin_user

from app.config import get_settings
from app.mcp_oauth import MCP_SCOPE, oauth_provider

router = APIRouter(tags=["openai-plugin"])
settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)

OFFLINE_ACCESS_SCOPE = "offline_access"
OPENID_SCOPE = "openid"
EMAIL_SCOPE = "email"
SUPPORTED_SCOPES = [MCP_SCOPE, OFFLINE_ACCESS_SCOPE, OPENID_SCOPE, EMAIL_SCOPE]


@router.get("/.well-known/openai-apps-challenge", include_in_schema=False)
async def openai_apps_challenge():
    token = settings.openai_apps_challenge_token
    if not token:
        return Response(status_code=404)
    return PlainTextResponse(content=token)


@router.get("/.well-known/openid-configuration", include_in_schema=False)
async def openid_configuration():
    issuer = settings.mcp_issuer_url.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/userinfo",
        "registration_endpoint": f"{issuer}/register",
        "revocation_endpoint": f"{issuer}/revoke",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "scopes_supported": SUPPORTED_SCOPES,
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }


@router.get("/.well-known/jwks.json", include_in_schema=False)
async def openid_jwks():
    # Immensity issues opaque OAuth access/refresh tokens, not signed JWT access tokens.
    # The empty JWK set keeps OIDC discovery parseable for clients that require jwks_uri.
    return {"keys": []}


@router.get("/userinfo", include_in_schema=False)
async def userinfo(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    access = await oauth_provider.load_access_token(credentials.credentials) if credentials else None
    if access is None or not access.subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scopes = set(access.scopes)
    if OPENID_SCOPE not in scopes or EMAIL_SCOPE not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="openid and email scopes are required",
        )

    user = await supabase_admin_user(access.subject)
    return {
        "sub": access.subject,
        "email": user.get("email"),
        "email_verified": bool(user.get("email_confirmed_at")),
    }
