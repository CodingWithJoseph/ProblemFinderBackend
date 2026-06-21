"""Admin-editable global settings.

A small whitelist of config knobs can be overridden at runtime by an admin and
persisted in the ``app_settings`` table; everything else stays env/config-only.

Reads go through :func:`effective_config`, which overlays saved overrides on top
of the :class:`Settings` defaults and otherwise behaves like the settings object
(unknown attributes fall through). The admin UI renders each knob from
:func:`serialize_settings` and saves through :func:`apply_settings`, which
validates every value against the whitelist (so the UI only ever needs
toggles/dropdowns — never free text).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AppSetting


@dataclass(frozen=True)
class SettingDef:
    key: str
    kind: str  # "choice" | "toggle" | "string_choice"
    label: str
    group: str
    options: tuple[int, ...] = ()
    str_options: tuple[str, ...] = ()
    unit: str | None = None

    def coerce(self, value: Any) -> Any:
        """Validate/normalize a value; raise ValueError if it's not allowed."""
        if self.kind == "toggle":
            if not isinstance(value, bool):
                raise ValueError(f"{self.key} expects a boolean")
            return value
        if self.kind == "string_choice":
            if not isinstance(value, str):
                raise ValueError(f"{self.key} expects a string")
            if value not in self.str_options:
                raise ValueError(f"{self.key} must be one of {list(self.str_options)}")
            return value
        # choice (integer from a fixed option set)
        if isinstance(value, bool):  # bool is an int subclass — reject explicitly
            raise ValueError(f"{self.key} expects an integer")
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.key} expects an integer")
        if ivalue not in self.options:
            raise ValueError(f"{self.key} must be one of {list(self.options)}")
        return ivalue


# The whitelist. Each key matches an attribute on Settings (its default).
SETTING_DEFS: tuple[SettingDef, ...] = (
    SettingDef("analytics_usage_window_days", "choice", "Usage window", "Analytics", (7, 14, 30), unit="days"),
    SettingDef("analytics_correlation_window_days", "choice", "Correlation window", "Analytics", (14, 30, 60, 90), unit="days"),
    SettingDef("analytics_retention_window_days", "choice", "Retention window", "Analytics", (14, 30, 60, 90), unit="days"),
    SettingDef("analytics_growth_window_days", "choice", "Growth comparison window", "Analytics", (7, 14, 30), unit="days"),
    SettingDef("revenue_sync_window_days", "choice", "Revenue sync window", "Revenue", (7, 30, 90), unit="days"),
    SettingDef("revenue_engine", "string_choice", "Revenue engine", "Revenue", str_options=("subscription", "invoice")),
    SettingDef("alert_error_baseline_days", "choice", "Error-spike baseline", "Alerts", (3, 7, 14), unit="days"),
    SettingDef("alert_new_issue_cap", "choice", "New-issue email cap", "Alerts", tuple(range(1, 11)), unit="per run"),
    SettingDef("stripe_oauth_state_ttl_minutes", "choice", "Stripe link lifetime", "Integrations", (5, 15, 30, 60), unit="minutes"),
    SettingDef("alerts_enabled", "toggle", "Monitoring alerts", "Alerts"),
)

_DEFS_BY_KEY = {d.key: d for d in SETTING_DEFS}


class EffectiveConfig:
    """Overlay returning an override when present, else the Settings default.
    Any other attribute falls through to Settings, so it's a drop-in wherever
    code used to read ``settings.<x>`` directly."""

    def __init__(self, overrides: dict[str, Any], settings) -> None:
        self._overrides = overrides
        self._settings = settings

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_settings"), name)


async def load_overrides(db: AsyncSession) -> dict[str, Any]:
    """Saved overrides, keyed by setting key. Stale/corrupt rows are ignored so a
    bad value can never break a read — it just falls back to the default."""
    rows = (await db.execute(select(AppSetting))).scalars().all()
    out: dict[str, Any] = {}
    for row in rows:
        definition = _DEFS_BY_KEY.get(row.key)
        if not definition:
            continue
        try:
            out[row.key] = definition.coerce(row.value)
        except ValueError:
            continue
    return out


async def effective_config(db: AsyncSession, settings=None) -> EffectiveConfig:
    return EffectiveConfig(await load_overrides(db), settings or get_settings())


async def serialize_settings(db: AsyncSession, settings=None) -> list[dict]:
    """Every knob's current value + UI metadata (control kind, options, unit)."""
    s = settings or get_settings()
    overrides = await load_overrides(db)
    return [
        {
            "key": d.key,
            "kind": d.kind,
            "label": d.label,
            "group": d.group,
            "options": list(d.options),
            "strOptions": list(d.str_options),
            "unit": d.unit,
            "value": overrides[d.key] if d.key in overrides else getattr(s, d.key),
            "isDefault": d.key not in overrides,
        }
        for d in SETTING_DEFS
    ]


async def apply_settings(db: AsyncSession, updates: dict[str, Any]) -> None:
    """Validate and persist overrides. Raises ValueError on an unknown key or a
    value outside its allowed set. The caller commits."""
    if not updates:
        return
    now = datetime.now(timezone.utc)
    for key, value in updates.items():
        definition = _DEFS_BY_KEY.get(key)
        if not definition:
            raise ValueError(f"Unknown setting: {key}")
        coerced = definition.coerce(value)
        existing = (await db.execute(
            select(AppSetting).where(AppSetting.key == key)
        )).scalar_one_or_none()
        if existing is None:
            db.add(AppSetting(key=key, value=coerced, updated_at=now))
        else:
            existing.value = coerced
            existing.updated_at = now
