import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import Issue, IssueComment, Pipeline, Team, TeamMember

router = APIRouter(prefix="/issues", tags=["issues"])

IssueStatus = Literal["open", "done", "archived"]
IssueType = Literal["issue", "kill_criteria"]

DEFAULT_PIPELINE_ISSUES = [
    ("analyze_signals", "Analyze signals", "Review the signal page and decide what evidence matters."),
    ("validate_breakdown_problems", "Validate breakdown problems", "Check whether the breakdown problems are specific and well supported."),
    ("create_tasks", "Create tasks", "Create prototype or concept tasks only when the opportunity is ready."),
]


class CreateIssueBody(BaseModel):
    title: str
    summary: str | None = None
    team_id: str | None = None
    pipeline_id: str | None = None
    parent_issue_id: str | None = None
    status: IssueStatus = "open"
    issue_type: IssueType = "issue"
    position: int = 0
    source: str | None = None


class PatchIssueBody(BaseModel):
    title: str | None = None
    summary: str | None = None
    status: IssueStatus | None = None
    issue_type: IssueType | None = None
    position: int | None = None


class CreateCommentBody(BaseModel):
    body: str


class PatchCommentBody(BaseModel):
    body: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _serialize_comment(comment: IssueComment) -> dict:
    return {
        "id": comment.id,
        "issueId": comment.issue_id,
        "userId": comment.user_id,
        "body": comment.body,
        "createdAt": _iso(comment.created_at),
        "updatedAt": _iso(comment.updated_at),
    }


def _serialize_issue(issue: Issue, comment_count: int = 0, sub_issue_count: int = 0) -> dict:
    return {
        "id": issue.id,
        "userId": issue.user_id,
        "teamId": issue.team_id,
        "pipelineId": issue.pipeline_id,
        "parentIssueId": issue.parent_issue_id,
        "title": issue.title,
        "summary": issue.summary,
        "status": issue.status,
        "issueType": issue.issue_type,
        "position": issue.position,
        "source": issue.source,
        "commentCount": comment_count,
        "subIssueCount": sub_issue_count,
        "createdAt": _iso(issue.created_at),
        "updatedAt": _iso(issue.updated_at),
        "closedAt": _iso(issue.closed_at),
    }


async def _pipeline_owned(pipeline_id: str, db: AsyncSession, uid: str) -> Pipeline | None:
    return (await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )).scalar_one_or_none()


async def _team_visible(team_id: str, db: AsyncSession, uid: str) -> bool:
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if not team:
        return False
    if team.owner_user_id == uid:
        return True
    member = (await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == team_id,
            TeamMember.user_id == uid,
            TeamMember.status != "removed",
        )
    )).scalar_one_or_none()
    return member is not None


async def _issue_visible(issue: Issue, db: AsyncSession, uid: str) -> bool:
    if issue.user_id == uid:
        return True
    if issue.pipeline_id and await _pipeline_owned(issue.pipeline_id, db, uid):
        return True
    if issue.team_id and await _team_visible(issue.team_id, db, uid):
        return True
    return False


async def _require_issue(issue_id: str, db: AsyncSession, uid: str) -> Issue:
    issue = (await db.execute(select(Issue).where(Issue.id == issue_id))).scalar_one_or_none()
    if not issue or not await _issue_visible(issue, db, uid):
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


async def _require_pipeline(pipeline_id: str | None, db: AsyncSession, uid: str) -> Pipeline | None:
    if not pipeline_id:
        return None
    pipeline = await _pipeline_owned(pipeline_id, db, uid)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    return pipeline


async def _require_team(team_id: str | None, db: AsyncSession, uid: str) -> None:
    if not team_id:
        return
    if not await _team_visible(team_id, db, uid):
        raise HTTPException(status_code=404, detail="Team not found")


def _visible_conditions(uid: str):
    pipeline_ids = select(Pipeline.id).where(Pipeline.user_id == uid)
    team_ids = select(TeamMember.team_id).where(TeamMember.user_id == uid, TeamMember.status != "removed")
    owned_team_ids = select(Team.id).where(Team.owner_user_id == uid)
    return or_(
        Issue.user_id == uid,
        Issue.pipeline_id.in_(pipeline_ids),
        Issue.team_id.in_(team_ids),
        Issue.team_id.in_(owned_team_ids),
    )


async def _counts(issue_ids: list[str], db: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    if not issue_ids:
        return {}, {}
    comment_rows = (await db.execute(
        select(IssueComment.issue_id, func.count(IssueComment.id))
        .where(IssueComment.issue_id.in_(issue_ids))
        .group_by(IssueComment.issue_id)
    )).all()
    sub_rows = (await db.execute(
        select(Issue.parent_issue_id, func.count(Issue.id))
        .where(Issue.parent_issue_id.in_(issue_ids), Issue.status != "archived")
        .group_by(Issue.parent_issue_id)
    )).all()
    return (
        {str(issue_id): int(count) for issue_id, count in comment_rows},
        {str(issue_id): int(count) for issue_id, count in sub_rows},
    )


async def _ensure_default_issues_for_pipeline(pipeline_id: str, db: AsyncSession, uid: str) -> None:
    pipeline = await _pipeline_owned(pipeline_id, db, uid)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    keys = [key for key, _, _ in DEFAULT_PIPELINE_ISSUES]
    existing_rows = (await db.execute(
        select(Issue.source).where(
            Issue.pipeline_id == pipeline_id,
            Issue.parent_issue_id == None,
            Issue.issue_type == "issue",
            Issue.source.in_(keys),
        )
    )).all()
    existing = {row[0] for row in existing_rows if row[0]}
    missing = [(key, title, summary) for key, title, summary in DEFAULT_PIPELINE_ISSUES if key not in existing]
    if not missing:
        return

    now = _now()
    for position, (key, title, summary) in enumerate(missing, start=len(existing)):
        db.add(Issue(
            id=str(uuid.uuid4()),
            user_id=uid,
            team_id=pipeline.team_id,
            pipeline_id=pipeline_id,
            title=title,
            summary=summary,
            status="open",
            issue_type="issue",
            position=position,
            source=key,
            created_at=now,
            updated_at=now,
        ))
    await db.commit()


async def _create_issue(body: CreateIssueBody, db: AsyncSession, uid: str, parent_issue_id: str | None = None) -> Issue:
    title = _clean(body.title)
    if not title:
        raise HTTPException(status_code=400, detail="Issue title is required")

    effective_parent_id = parent_issue_id or body.parent_issue_id
    parent = None
    if effective_parent_id:
        parent = await _require_issue(effective_parent_id, db, uid)

    pipeline_id = body.pipeline_id or (parent.pipeline_id if parent else None)
    team_id = body.team_id or (parent.team_id if parent else None)
    pipeline = await _require_pipeline(pipeline_id, db, uid)
    if team_id is None and pipeline is not None:
        team_id = pipeline.team_id
    await _require_team(team_id, db, uid)

    now = _now()
    issue = Issue(
        id=str(uuid.uuid4()),
        user_id=uid,
        team_id=team_id,
        pipeline_id=pipeline_id,
        parent_issue_id=effective_parent_id,
        title=title,
        summary=_clean(body.summary),
        status=body.status,
        issue_type=body.issue_type,
        position=body.position,
        source=_clean(body.source),
        created_at=now,
        updated_at=now,
        closed_at=now if body.status in {"done", "archived"} else None,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


@router.get("")
async def list_issues(
    pipeline_id: str | None = Query(None),
    team_id: str | None = Query(None),
    status: IssueStatus | None = Query(None),
    issue_type: IssueType | None = Query(None),
    parent_issue_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    if pipeline_id:
        await _ensure_default_issues_for_pipeline(pipeline_id, db, uid)

    conditions = [_visible_conditions(uid)]
    if pipeline_id:
        conditions.append(Issue.pipeline_id == pipeline_id)
    if team_id:
        conditions.append(Issue.team_id == team_id)
    if status:
        conditions.append(Issue.status == status)
    if issue_type:
        conditions.append(Issue.issue_type == issue_type)
    if parent_issue_id:
        conditions.append(Issue.parent_issue_id == parent_issue_id)
    else:
        conditions.append(Issue.parent_issue_id == None)

    issues = list((await db.execute(
        select(Issue)
        .where(and_(*conditions))
        .order_by(Issue.position.asc(), Issue.created_at.desc())
    )).scalars().all())
    comment_counts, sub_counts = await _counts([issue.id for issue in issues], db)
    return {
        "data": [
            _serialize_issue(issue, comment_counts.get(issue.id, 0), sub_counts.get(issue.id, 0))
            for issue in issues
        ]
    }


@router.post("")
async def create_issue(
    body: CreateIssueBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _create_issue(body, db, uid)
    return {"data": _serialize_issue(issue)}


@router.get("/{issue_id}")
async def get_issue(
    issue_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    comments = list((await db.execute(
        select(IssueComment).where(IssueComment.issue_id == issue.id).order_by(IssueComment.created_at.asc())
    )).scalars().all())
    sub_issues = list((await db.execute(
        select(Issue)
        .where(Issue.parent_issue_id == issue.id, Issue.status != "archived")
        .order_by(Issue.position.asc(), Issue.created_at.asc())
    )).scalars().all())
    comment_counts, sub_counts = await _counts([issue.id], db)
    return {
        "data": {
            **_serialize_issue(issue, comment_counts.get(issue.id, 0), sub_counts.get(issue.id, 0)),
            "comments": [_serialize_comment(comment) for comment in comments],
            "subIssues": [_serialize_issue(child) for child in sub_issues],
        }
    }


@router.patch("/{issue_id}")
async def patch_issue(
    issue_id: str,
    body: PatchIssueBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    updates = body.model_dump(exclude_unset=True)
    if "title" in updates:
        title = _clean(body.title)
        if not title:
            raise HTTPException(status_code=400, detail="Issue title is required")
        issue.title = title
    if "summary" in updates:
        issue.summary = _clean(body.summary)
    if "position" in updates and body.position is not None:
        issue.position = body.position
    if "status" in updates and body.status:
        issue.status = body.status
        issue.closed_at = _now() if body.status in {"done", "archived"} else None
    if "issue_type" in updates and body.issue_type:
        issue.issue_type = body.issue_type
    issue.updated_at = _now()
    await db.commit()
    await db.refresh(issue)
    return {"data": _serialize_issue(issue)}


@router.delete("/{issue_id}")
async def delete_issue(
    issue_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    now = _now()
    issue.status = "archived"
    issue.closed_at = now
    issue.updated_at = now
    await db.commit()
    return {"success": True}


@router.get("/{issue_id}/comments")
async def list_comments(
    issue_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    comments = list((await db.execute(
        select(IssueComment).where(IssueComment.issue_id == issue.id).order_by(IssueComment.created_at.asc())
    )).scalars().all())
    return {"data": [_serialize_comment(comment) for comment in comments]}


@router.post("/{issue_id}/comments")
async def create_comment(
    issue_id: str,
    body: CreateCommentBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    text = _clean(body.body)
    if not text:
        raise HTTPException(status_code=400, detail="Comment body is required")
    now = _now()
    comment = IssueComment(
        id=str(uuid.uuid4()),
        issue_id=issue.id,
        user_id=uid,
        body=text,
        created_at=now,
        updated_at=now,
    )
    db.add(comment)
    issue.updated_at = now
    await db.commit()
    await db.refresh(comment)
    return {"data": _serialize_comment(comment)}


async def _require_comment(issue: Issue, comment_id: str, db: AsyncSession, uid: str) -> IssueComment:
    comment = (await db.execute(
        select(IssueComment).where(IssueComment.issue_id == issue.id, IssueComment.id == comment_id)
    )).scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != uid and issue.user_id != uid:
        raise HTTPException(status_code=403, detail="Cannot edit this comment")
    return comment


@router.patch("/{issue_id}/comments/{comment_id}")
async def patch_comment(
    issue_id: str,
    comment_id: str,
    body: PatchCommentBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    comment = await _require_comment(issue, comment_id, db, uid)
    text = _clean(body.body)
    if not text:
        raise HTTPException(status_code=400, detail="Comment body is required")
    comment.body = text
    comment.updated_at = _now()
    await db.commit()
    await db.refresh(comment)
    return {"data": _serialize_comment(comment)}


@router.delete("/{issue_id}/comments/{comment_id}")
async def delete_comment(
    issue_id: str,
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    issue = await _require_issue(issue_id, db, uid)
    comment = await _require_comment(issue, comment_id, db, uid)
    await db.delete(comment)
    await db.commit()
    return {"success": True}


@router.post("/{issue_id}/subissues")
async def create_sub_issue(
    issue_id: str,
    body: CreateIssueBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    child = await _create_issue(body, db, uid, parent_issue_id=issue_id)
    return {"data": _serialize_issue(child)}
