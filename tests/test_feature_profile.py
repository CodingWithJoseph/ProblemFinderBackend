from app.feature_profile import (
    CORE_ROUTERS,
    DEFERRED_ROUTERS,
    deferred_features_enabled,
    normalize_profile,
)


def test_normalize_fails_open_to_full():
    # Misconfiguration must never silently hide the product.
    assert normalize_profile("core") == "core"
    assert normalize_profile("  CORE ") == "core"
    for bad in ("full", "", None, "prod", "core-ish"):
        assert normalize_profile(bad) == "full"


def test_deferred_enabled_only_under_full():
    assert deferred_features_enabled("full") is True
    assert deferred_features_enabled(None) is True
    assert deferred_features_enabled("core") is False


def test_core_loop_routers_are_never_deferred():
    # The initial-release journey: search -> breakdown -> tasks -> pipeline
    # (portfolio backs the board's Launched column).
    for essential in ("clusters", "problems", "tasks", "pipeline", "portfolio", "subscriptions"):
        assert essential in CORE_ROUTERS
        assert essential not in DEFERRED_ROUTERS


def test_deferred_covers_the_post_launch_surface():
    for deferred in ("goals", "teams", "issues", "monitor", "monitor_ingest"):
        assert deferred in DEFERRED_ROUTERS
    assert not (CORE_ROUTERS & DEFERRED_ROUTERS)


def test_main_gates_every_deferred_mount():
    """Source-level guard: each deferred router's include must sit inside a
    deferred_features_enabled block; core mounts must not."""
    src = open("main.py").read()
    blocks = src.split("if deferred_features_enabled(settings.feature_profile):")
    ungated = blocks[0] + "".join(
        "\n".join(line for line in b.split("\n") if not line.startswith("    "))
        for b in blocks[1:]
    )
    for name in ("goals_router", "monitor_router", "monitor_war_room_router",
                 "monitor_public_router", "teams_router", "invites_router", "issues_router"):
        assert f"app.include_router({name}" not in ungated, f"{name} mounted outside the profile gate"
    for name in ("clusters_router", "problems_router", "tasks_router",
                 "pipeline_router", "portfolio_router"):
        assert f"app.include_router({name}" in ungated, f"{name} must mount in every profile"
