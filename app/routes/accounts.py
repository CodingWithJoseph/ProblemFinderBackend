"""Application profiles, onboarding, deletion and waitlist in PostgreSQL."""

import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_claims, get_uid, require_recent_user
from app.account_deletion import finish_account_deletion
from app.db import get_db
from app.models import (
    AccountProfile, LaunchWaitlist, McpOAuthCode, McpOAuthToken, Pipeline, Problem,
    SearchSession, SignalAnalysisCase, SolutionCase,
)

router = APIRouter(tags=["accounts"])


def initial_name(claims: dict) -> str:
    metadata = claims.get("user_metadata") or {}
    if not isinstance(metadata, dict):
        return ""
    display = metadata.get("display_name")
    if not isinstance(display, str):
        display = " ".join(str(metadata.get(key) or "") for key in ("first_name", "last_name")).strip()
    return display.strip()[:160]


async def profile_for(db: AsyncSession, uid: str, claims: dict) -> AccountProfile:
    await db.execute(insert(AccountProfile).values(
        id=uid, display_name=initial_name(claims), onboarding_complete=False, focus_areas=[],
    ).on_conflict_do_nothing(index_elements=[AccountProfile.id]))
    profile = (await db.execute(select(AccountProfile).where(AccountProfile.id == uid))).scalar_one()
    if profile.deleted_at is not None:
        raise HTTPException(403, detail="Account has been deleted")
    return profile


def profile_response(profile: AccountProfile) -> dict:
    return {
        "id": profile.id, "displayName": profile.display_name,
        "onboardingComplete": profile.onboarding_complete, "focusAreas": profile.focus_areas,
    }


class ProfileUpdate(BaseModel):
    displayName: str = Field(max_length=160)


@router.get("/account")
async def read_account(request: Request, uid: str = Depends(get_uid), db: AsyncSession = Depends(get_db)):
    return profile_response(await profile_for(db, uid, request.state.auth_claims))


@router.patch("/account")
async def update_account(body: ProfileUpdate, request: Request, uid: str = Depends(get_uid), db: AsyncSession = Depends(get_db)):
    profile = await profile_for(db, uid, request.state.auth_claims)
    profile.display_name = body.displayName.strip()
    profile.updated_at = datetime.now(timezone.utc)
    return profile_response(profile)


@router.get("/onboarding/status")
async def onboarding_status(request: Request, uid: str = Depends(get_uid), db: AsyncSession = Depends(get_db)):
    profile = await profile_for(db, uid, request.state.auth_claims)
    return {"complete": profile.onboarding_complete}


class OnboardingUpdate(BaseModel):
    focusAreas: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("focusAreas")
    @classmethod
    def normalize_focus(cls, values: list[str]) -> list[str]:
        if any(len(value) > 100 for value in values):
            raise ValueError("Focus areas must be at most 100 characters")
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


@router.post("/onboarding")
async def complete_onboarding(request: Request, body: OnboardingUpdate | None = None, uid: str = Depends(get_uid), db: AsyncSession = Depends(get_db)):
    profile = await profile_for(db, uid, request.state.auth_claims)
    profile.onboarding_complete = True
    profile.focus_areas = body.focusAreas if body else []
    profile.updated_at = datetime.now(timezone.utc)
    return {"success": True}


@router.delete("/account")
async def delete_account(request: Request, claims: dict = Depends(get_claims), db: AsyncSession = Depends(get_db)):
    uid = claims["sub"]
    profile = (await db.execute(select(AccountProfile).where(AccountProfile.id == uid))).scalar_one_or_none()
    if profile is not None and profile.deletion_completed_at is not None:
        return {"success": True}
    # Check Admin configuration before fencing an account we cannot remove.
    from app.auth import settings
    if not settings.supabase_secret_key:
        raise HTTPException(503, detail="Account administration is not configured")
    if profile is None or profile.deleted_at is None:
        await require_recent_user(request, claims)
    # A retry for a fenced account still requires a valid signed token for that UUID.
    # It may already have been removed from Auth after a previous interrupted attempt.
    now = datetime.now(timezone.utc)
    await db.execute(insert(AccountProfile).values(
        id=uid, display_name="", focus_areas=[], onboarding_complete=False,
    ).on_conflict_do_nothing(index_elements=[AccountProfile.id]))
    await db.execute(update(AccountProfile).where(AccountProfile.id == uid).values(
        deleted_at=now, display_name="", focus_areas=[], updated_at=now,
    ))
    await db.execute(update(McpOAuthToken).where(McpOAuthToken.user_id == uid).values(revoked_at=now))
    await db.execute(delete(McpOAuthCode).where(McpOAuthCode.user_id == uid))
    await db.commit()  # Durable deny fence survives a provider or cleanup failure.

    await finish_account_deletion(db, uid)
    return {"success": True}


class WaitlistRequest(BaseModel):
    email: str = Field(max_length=320)
    source: str = Field(default="unknown", max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("Please enter a valid email address")
        return normalized


@router.post("/waitlist")
async def subscribe_waitlist(body: WaitlistRequest, db: AsyncSession = Depends(get_db)):
    key = hashlib.sha256(body.email.encode()).hexdigest()
    source = re.sub(r"[^a-z0-9_-]", "-", body.source.strip().lower())[:64] or "unknown"
    inserted = (await db.execute(insert(LaunchWaitlist).values(
        id=key, email=body.email, source=source, consent_version="launch-updates-v1",
    ).on_conflict_do_nothing(index_elements=[LaunchWaitlist.id]).returning(LaunchWaitlist.id))).scalar_one_or_none()
    if inserted is None:
        await db.execute(update(LaunchWaitlist).where(LaunchWaitlist.id == key).values(
            source=source, updated_at=datetime.now(timezone.utc),
        ))
    return {"success": True, "alreadySubscribed": inserted is None}
