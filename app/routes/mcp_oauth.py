"""Browser handoff endpoints for approving a hosted MCP connection."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_uid
from app.mcp_oauth import oauth_provider

router = APIRouter(prefix="/oauth/authorize", tags=["mcp-oauth"])


class CompleteAuthorizationResponse(BaseModel):
    redirectUrl: str


@router.get("/{request_id}")
async def describe_authorization(request_id: str):
    request = await oauth_provider.describe_pending_authorization(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Connection request not found or expired")
    return request


@router.post("/{request_id}/complete", response_model=CompleteAuthorizationResponse)
async def complete_authorization(request_id: str, uid: str = Depends(get_uid)):
    redirect_url = await oauth_provider.complete_authorization(request_id, uid)
    return CompleteAuthorizationResponse(redirectUrl=redirect_url)
