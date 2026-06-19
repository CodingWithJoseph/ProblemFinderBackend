import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.config import get_settings
from app.db import get_db
from app.models import Team, TeamMember
from app.services.email import email_service, render_team_invite

settings = get_settings()

router = APIRouter(prefix="/teams", tags=["teams"])
invites_router = APIRouter(prefix="/invites", tags=["invites"])

TeamRole = Literal["owner", "admin", "member"]
MemberStatus = Literal["active", "invited", "removed"]


class CreateTeamBody(BaseModel):
    name: str
    description: str | None = None


class PatchTeamBody(BaseModel):
    name: str | None = None
    description: str | None = None


class AddMemberBody(BaseModel):
    user_id: str | None = None
    email: str | None = None
    role: Literal["admin", "member"] = "member"
    status: MemberStatus | None = None


class PatchMemberBody(BaseModel):
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    role: Literal["admin", "member"] | None = None
    status: MemberStatus | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _display_name_from_email(email: str | None) -> str | None:
    if not email:
        return None
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
    name = " ".join(part.capitalize() for part in local.split() if part)
    return name or None


def _accept_url(token: str) -> str:
    return f"{settings.app_url.rstrip('/')}/invite/{token}"


def _serialize_member(member: TeamMember, *, include_invite: bool = False) -> dict:
    payload = {
        "id": member.id,
        "teamId": member.team_id,
        "userId": member.user_id,
        "email": member.email,
        "displayName": member.display_name,
        "role": member.role,
        "status": member.status,
        "invitedAt": _iso(member.invited_at),
        "createdAt": _iso(member.created_at),
        "updatedAt": _iso(member.updated_at),
    }
    # The accept link carries a secret join token, so only expose it to callers
    # allowed to manage members (owners/admins). Pending email invites only.
    if include_invite and member.status == "invited" and member.invite_token:
        payload["inviteUrl"] = _accept_url(member.invite_token)
    return payload


def _issue_invite_token(member: TeamMember, now: datetime) -> str:
    """Mint a fresh single-use invite token on ``member`` and return it."""
    token = secrets.token_urlsafe(32)
    member.invite_token = token
    member.invited_at = now
    member.invite_token_expires_at = now + timedelta(hours=settings.invite_token_ttl_hours)
    return token


def _queue_invite_email(background: BackgroundTasks, *, team: Team, email: str, token: str) -> None:
    """Schedule the invite email after the response. Sending is non-fatal."""
    subject, text, html = render_team_invite(team_name=team.name, accept_url=_accept_url(token))
    background.add_task(email_service.send, to=email, subject=subject, text=text, html=html)


def _invite_expired(member: TeamMember, now: datetime) -> bool:
    return member.invite_token_expires_at is not None and member.invite_token_expires_at < now


async def _member_by_token(token: str, db: AsyncSession) -> TeamMember:
    member = (await db.execute(
        select(TeamMember).where(TeamMember.invite_token == token)
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Invite not found")
    return member


def _serialize_team(team: Team, role: str | None = None, members: list[TeamMember] | None = None, include_invites: bool = False) -> dict:
    payload = {
        "id": team.id,
        "ownerUserId": team.owner_user_id,
        "name": team.name,
        "description": team.description,
        "role": role,
        "createdAt": _iso(team.created_at),
        "updatedAt": _iso(team.updated_at),
    }
    if members is not None:
        payload["members"] = [_serialize_member(member, include_invite=include_invites) for member in members]
    return payload


async def _get_team(team_id: str, db: AsyncSession) -> Team:
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


async def _membership(team_id: str, uid: str, db: AsyncSession) -> TeamMember | None:
    return (await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == team_id,
            TeamMember.user_id == uid,
            TeamMember.status != "removed",
        )
    )).scalar_one_or_none()


async def _role_for_user(team: Team, uid: str, db: AsyncSession) -> str | None:
    if team.owner_user_id == uid:
        return "owner"
    member = await _membership(team.id, uid, db)
    return member.role if member else None


async def _require_view(team_id: str, uid: str, db: AsyncSession) -> tuple[Team, str]:
    team = await _get_team(team_id, db)
    role = await _role_for_user(team, uid, db)
    if not role:
        raise HTTPException(status_code=404, detail="Team not found")
    return team, role


async def _require_owner(team_id: str, uid: str, db: AsyncSession) -> Team:
    team = await _get_team(team_id, db)
    if team.owner_user_id != uid:
        raise HTTPException(status_code=403, detail="Only the team owner can do this")
    return team


async def _require_member_manager(team_id: str, uid: str, db: AsyncSession) -> tuple[Team, str]:
    team, role = await _require_view(team_id, uid, db)
    if role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only owners and admins can manage members")
    return team, role


async def _members_for_team(team_id: str, db: AsyncSession) -> list[TeamMember]:
    return list((await db.execute(
        select(TeamMember)
        .where(TeamMember.team_id == team_id, TeamMember.status != "removed")
        .order_by(TeamMember.created_at.asc())
    )).scalars().all())


async def _existing_member(
    team_id: str, db: AsyncSession, *, user_id: str | None = None, email: str | None = None
) -> TeamMember | None:
    """A non-removed member of the team matching the given user_id or email
    (email compared case-insensitively). Used to reject duplicate invites."""
    conditions = []
    if user_id:
        conditions.append(TeamMember.user_id == user_id)
    if email:
        conditions.append(func.lower(TeamMember.email) == email.lower())
    if not conditions:
        return None
    return (await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == team_id,
            TeamMember.status != "removed",
            or_(*conditions),
        )
    )).scalars().first()


async def _member_for_team(team_id: str, member_id: str, db: AsyncSession) -> TeamMember:
    member = (await db.execute(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.id == member_id)
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")
    return member


@router.get("")
async def list_teams(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    rows = (await db.execute(
        select(Team)
        .outerjoin(TeamMember, Team.id == TeamMember.team_id)
        .where(
            or_(
                Team.owner_user_id == uid,
                and_(TeamMember.user_id == uid, TeamMember.status != "removed"),
            )
        )
        .distinct()
        .order_by(Team.updated_at.desc())
    )).scalars().all()
    return {"data": [_serialize_team(team, role=("owner" if team.owner_user_id == uid else "member")) for team in rows]}


@router.post("")
async def create_team(
    body: CreateTeamBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    name = _clean_text(body.name)
    if not name:
        raise HTTPException(status_code=400, detail="Team name is required")

    now = _now()
    team = Team(
        id=str(uuid.uuid4()),
        owner_user_id=uid,
        name=name,
        description=_clean_text(body.description),
        created_at=now,
        updated_at=now,
    )
    owner = TeamMember(
        id=str(uuid.uuid4()),
        team_id=team.id,
        user_id=uid,
        role="owner",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(team)
    db.add(owner)
    await db.commit()
    await db.refresh(team)
    await db.refresh(owner)
    return {"data": _serialize_team(team, role="owner", members=[owner])}


@router.get("/{team_id}")
async def get_team(
    team_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team, role = await _require_view(team_id, uid, db)
    members = await _members_for_team(team.id, db)
    return {"data": _serialize_team(team, role=role, members=members, include_invites=role in {"owner", "admin"})}


@router.patch("/{team_id}")
async def patch_team(
    team_id: str,
    body: PatchTeamBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team = await _require_owner(team_id, uid, db)
    updates = body.model_dump(exclude_unset=True)

    if "name" in updates:
        name = _clean_text(body.name)
        if not name:
            raise HTTPException(status_code=400, detail="Team name is required")
        team.name = name
    if "description" in updates:
        team.description = _clean_text(body.description)

    team.updated_at = _now()
    await db.commit()
    await db.refresh(team)
    members = await _members_for_team(team.id, db)
    return {"data": _serialize_team(team, role="owner", members=members, include_invites=True)}


@router.delete("/{team_id}")
async def delete_team(
    team_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team = await _require_owner(team_id, uid, db)
    await db.delete(team)
    await db.commit()
    return {"success": True}


@router.post("/{team_id}/members")
async def add_member(
    team_id: str,
    body: AddMemberBody,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team, _ = await _require_member_manager(team_id, uid, db)
    user_id = _clean_text(body.user_id)
    email = _clean_text(body.email)
    if not user_id and not email:
        raise HTTPException(status_code=400, detail="Member user_id or email is required")

    # Guard against duplicates (e.g. a double-clicked invite button): if this
    # person is already on the team, return 409 instead of a second membership.
    existing = await _existing_member(team.id, db, user_id=user_id, email=email)
    if existing:
        who = existing.email or existing.user_id or "This person"
        state = "already a member" if existing.status == "active" else "already invited"
        raise HTTPException(status_code=409, detail=f"{who} is {state}")

    now = _now()
    status = body.status or ("active" if user_id else "invited")
    member = TeamMember(
        id=str(uuid.uuid4()),
        team_id=team.id,
        user_id=user_id,
        email=email,
        display_name=_display_name_from_email(email),
        role=body.role,
        status=status,
        created_at=now,
        updated_at=now,
    )
    # Email-only invite (no linked account yet) → mint a token and send the link.
    invite_token = None
    if email and not user_id and status == "invited":
        invite_token = _issue_invite_token(member, now)
    team.updated_at = now
    db.add(member)
    await db.commit()
    await db.refresh(member)
    if invite_token:
        _queue_invite_email(background, team=team, email=email, token=invite_token)
    return {"data": _serialize_member(member, include_invite=True)}


@router.patch("/{team_id}/members/{member_id}")
async def patch_member(
    team_id: str,
    member_id: str,
    body: PatchMemberBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team, _ = await _require_member_manager(team_id, uid, db)
    member = await _member_for_team(team.id, member_id, db)
    if member.role == "owner":
        raise HTTPException(status_code=400, detail="Owner membership cannot be changed")

    updates = body.model_dump(exclude_unset=True)
    if "user_id" in updates:
        member.user_id = _clean_text(body.user_id)
    if "email" in updates:
        member.email = _clean_text(body.email)
    if "display_name" in updates:
        member.display_name = _clean_text(body.display_name)
    if "role" in updates and body.role:
        member.role = body.role
    if "status" in updates and body.status:
        member.status = body.status

    now = _now()
    member.updated_at = now
    team.updated_at = now
    await db.commit()
    await db.refresh(member)
    return {"data": _serialize_member(member, include_invite=True)}


@router.delete("/{team_id}/members/{member_id}")
async def delete_member(
    team_id: str,
    member_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team, _ = await _require_member_manager(team_id, uid, db)
    member = await _member_for_team(team.id, member_id, db)
    if member.role == "owner":
        raise HTTPException(status_code=400, detail="Owner membership cannot be removed")

    team.updated_at = _now()
    await db.delete(member)
    await db.commit()
    return {"success": True}


@router.post("/{team_id}/members/{member_id}/resend")
async def resend_invite(
    team_id: str,
    member_id: str,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    team, _ = await _require_member_manager(team_id, uid, db)
    member = await _member_for_team(team.id, member_id, db)
    if member.status != "invited" or not member.email:
        raise HTTPException(status_code=400, detail="Only pending email invites can be resent")

    now = _now()
    token = _issue_invite_token(member, now)
    member.updated_at = now
    team.updated_at = now
    await db.commit()
    await db.refresh(member)
    _queue_invite_email(background, team=team, email=member.email, token=token)
    return {"data": _serialize_member(member, include_invite=True)}


@invites_router.get("/{token}")
async def get_invite(token: str, db: AsyncSession = Depends(get_db)):
    """Public preview of an invite, used to render the accept page."""
    member = await _member_by_token(token, db)
    team = await _get_team(member.team_id, db)
    now = _now()
    return {"data": {
        "token": token,
        "teamId": team.id,
        "teamName": team.name,
        "teamDescription": team.description,
        "email": member.email,
        "role": member.role,
        "status": member.status,
        "expiresAt": _iso(member.invite_token_expires_at),
        "expired": _invite_expired(member, now),
    }}


@invites_router.post("/{token}/accept")
async def accept_invite(
    token: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Bind the signed-in user to the invited membership."""
    member = await _member_by_token(token, db)
    now = _now()
    if member.status == "active":
        raise HTTPException(status_code=409, detail="Invite has already been accepted")
    if _invite_expired(member, now):
        raise HTTPException(status_code=410, detail="Invite has expired")

    member.user_id = uid
    member.status = "active"
    member.invite_token = None
    member.invite_token_expires_at = None
    member.updated_at = now
    team = await _get_team(member.team_id, db)
    team.updated_at = now
    await db.commit()
    await db.refresh(member)
    return {"data": _serialize_member(member)}
