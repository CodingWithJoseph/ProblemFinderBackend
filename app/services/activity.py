from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserActivityDaily


TRACKED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
TRACKED_PATH_PREFIXES = (
    "/pipeline",
    "/tasks",
    "/problems",
    "/issues",
    "/portfolio",
    "/teams",
)


def should_record_user_action(method: str, path: str, status_code: int) -> bool:
    return (
        method.upper() in TRACKED_METHODS
        and 200 <= status_code < 400
        and any(path == prefix or path.startswith(f"{prefix}/") for prefix in TRACKED_PATH_PREFIXES)
    )


async def record_user_activity(
    db: AsyncSession,
    user_id: str,
    kind: str,
    *,
    occurred_at: datetime | None = None,
) -> None:
    if kind not in {"login", "action"}:
        raise ValueError(f"Unsupported activity kind: {kind}")

    now = occurred_at or datetime.now(timezone.utc)
    login_increment = 1 if kind == "login" else 0
    action_increment = 1 if kind == "action" else 0
    statement = insert(UserActivityDaily).values(
        user_id=user_id,
        activity_date=now.date(),
        login_count=login_increment,
        action_count=action_increment,
        last_active_at=now,
        created_at=now,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[UserActivityDaily.user_id, UserActivityDaily.activity_date],
        set_={
            "login_count": UserActivityDaily.login_count + login_increment,
            "action_count": UserActivityDaily.action_count + action_increment,
            "last_active_at": now,
            "updated_at": now,
        },
    )
    await db.execute(statement)
