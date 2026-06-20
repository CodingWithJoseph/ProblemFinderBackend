"""Per-user preferences: notification delivery + workspace defaults.

Stored one row per uid in ``user_preferences``; absence means all defaults. The
alert scheduler consults :func:`notification_prefs_for` to decide whether (and
where) to email, and whether a product's alerts go out instantly or get rolled
into a daily/weekly digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserPreference

VALID_CADENCES = ("instant", "daily", "weekly")


@dataclass
class NotificationPrefs:
    """Effective notification settings for one user."""

    alerts_email_enabled: bool = True
    digest_cadence: str = "instant"
    alert_email: str | None = None

    @classmethod
    def from_row(cls, row: UserPreference | None) -> "NotificationPrefs":
        if row is None:
            return cls()
        return cls(
            alerts_email_enabled=True if row.alerts_email_enabled is None else row.alerts_email_enabled,
            digest_cadence=row.digest_cadence or "instant",
            alert_email=row.alert_email,
        )

    @property
    def send_instantly(self) -> bool:
        return self.alerts_email_enabled and self.digest_cadence == "instant"


def digest_interval(cadence: str) -> timedelta | None:
    """How often a digest goes out, or None for instant (no digest)."""
    if cadence == "daily":
        return timedelta(days=1)
    if cadence == "weekly":
        return timedelta(days=7)
    return None


async def get_preference_row(db: AsyncSession, uid: str) -> UserPreference | None:
    return (await db.execute(
        select(UserPreference).where(UserPreference.uid == uid)
    )).scalar_one_or_none()


async def notification_prefs_for(db: AsyncSession, uid: str) -> NotificationPrefs:
    return NotificationPrefs.from_row(await get_preference_row(db, uid))
