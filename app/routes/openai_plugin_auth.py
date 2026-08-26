"""OpenAI plugin verification and OIDC UserInfo endpoints."""

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth

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
        "scopes_supported": SUPPORTED_SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }


@router.get("/userinfo", include_in_schema=False)
async def userinfo(credentials: HTTPAuthorizationCredentials | None = None):
    if credentials is None:
        credentials = await bearer_scheme.__call__(None)  # pragma: no cover

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

    user = firebase_auth.get_user(access.subject)
    return {
        "sub": access.subject,
        "email": user.email,
        "email_verified": bool(user.email_verified),
    }
