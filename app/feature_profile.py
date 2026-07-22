"""Central feature-profile policy for controlling the released product surface."""

from fastapi import Depends, HTTPException

from app.config import Settings, get_settings

VALID_PROFILES = ("core", "full")
LOCAL_ENVIRONMENTS = ("development", "dev", "local", "test", "testing")

# Routers that are available in the MVP profile. Portfolio and subscriptions
# contain a small core read surface; their deferred operations are protected by
# ``require_deferred_features`` at the route level.
CORE_ROUTERS = (
    "subscriptions",
    "clusters",
    "search_sessions",
    "pipeline",
    "portfolio",
    "problems",
    "tasks",
    "dashboard",
    "homepage",
    "public",
    "preferences",
)

DEFERRED_ROUTERS = (
    "goals",
    "teams",
    "invites",
    "issues",
    "monitor",
    "monitor_war_room",
    "monitor_ingest",
)


def resolve_profile(value: str | None, environment: str) -> str:
    """Resolve a profile, allowing an ergonomic default only in local/test use.

    Staging and production must opt into ``core`` or ``full`` explicitly. This
    makes a missing or misspelled deployment setting stop application startup
    instead of accidentally exposing the deferred surface.
    """
    profile = (value or "").strip().lower()
    if profile in VALID_PROFILES:
        return profile

    normalized_environment = (environment or "").strip().lower()
    if normalized_environment in LOCAL_ENVIRONMENTS:
        return "full"

    raise RuntimeError(
        "FEATURE_PROFILE must be explicitly set to 'core' or 'full' "
        f"when ENVIRONMENT is {environment!r}"
    )


def deferred_features_enabled(profile: str) -> bool:
    return profile == "full"


def require_deferred_features(settings: Settings = Depends(get_settings)) -> None:
    """Return a neutral 404 when a deferred operation is unavailable."""
    profile = resolve_profile(settings.feature_profile, settings.environment)
    if not deferred_features_enabled(profile):
        raise HTTPException(status_code=404, detail="Feature unavailable")
