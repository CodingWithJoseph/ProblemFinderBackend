"""Release feature profiles.

The initial release ships the core loop — find problems, break them down, turn
them into buildable tasks — while everything already built for the wider product
(monitoring, goals, teams, issues) stays in the codebase but unmounted. One env
var (``FEATURE_PROFILE``) decides; branches never diverge in code.

Profiles:
    full  (default) — every router mounts; behavior identical to before this
                      module existed.
    core            — only the core-loop routers mount.

Kept under ``core`` and why:
    clusters / problems / tasks / pipeline  — the core loop itself
    portfolio                               — the pipeline board's Launched
                                              column lists via /portfolio; its
                                              revenue endpoints ride along but
                                              are auth+ownership gated and
                                              unreferenced by the core UI
    subscriptions / dashboard / homepage / public / preferences — infrastructure

Deferred under ``core``:
    goals (incl. getting-started), teams + invites, issues,
    monitor + war room, and the public monitor ingest beacons — deferring the
    beacons also removes unauthenticated public endpoints from the release.
"""

VALID_PROFILES = ("core", "full")


def normalize_profile(value: str | None) -> str:
    """Anything that isn't exactly ``core`` runs as ``full`` — misconfiguration
    fails open to the complete product rather than silently hiding features."""
    return "core" if (value or "").strip().lower() == "core" else "full"


def deferred_features_enabled(profile: str | None) -> bool:
    return normalize_profile(profile) == "full"


# Names are documentation + test surface; main.py stays the single mount point.
CORE_ROUTERS = frozenset({
    "subscriptions", "clusters", "pipeline", "portfolio",
    "problems", "tasks", "dashboard", "homepage", "public", "preferences",
})
DEFERRED_ROUTERS = frozenset({
    "goals", "teams", "invites", "issues",
    "monitor", "monitor_war_room", "monitor_ingest",
})
