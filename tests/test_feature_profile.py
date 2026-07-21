from pathlib import Path

import pytest
from fastapi import HTTPException

from app.feature_profile import (
    CORE_ROUTERS,
    DEFERRED_ROUTERS,
    deferred_features_enabled,
    require_deferred_features,
    resolve_profile,
)


class _Settings:
    def __init__(self, profile: str | None, environment: str):
        self.feature_profile = profile
        self.environment = environment


def test_explicit_profiles_are_resolved():
    assert resolve_profile("core", "production") == "core"
    assert resolve_profile(" FULL ", "staging") == "full"


def test_local_and_test_environments_default_to_full():
    assert resolve_profile(None, "development") == "full"
    assert resolve_profile("typo", "test") == "full"


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("value", [None, "", "typo"])
def test_deployments_reject_missing_or_invalid_profiles(environment, value):
    with pytest.raises(RuntimeError, match="FEATURE_PROFILE"):
        resolve_profile(value, environment)


def test_deferred_feature_switch():
    assert deferred_features_enabled("full") is True
    assert deferred_features_enabled("core") is False


def test_core_and_deferred_router_sets_do_not_overlap():
    assert set(CORE_ROUTERS).isdisjoint(DEFERRED_ROUTERS)


def test_core_dependency_returns_not_found():
    with pytest.raises(HTTPException) as exc:
        require_deferred_features(_Settings("core", "production"))
    assert exc.value.status_code == 404


def test_main_only_mounts_deferred_routers_inside_profile_guards():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    for router_name in ("goals", "monitor", "monitor_war_room", "monitor_public", "teams", "invites", "issues"):
        mount = f"app.include_router({router_name}_router"
        assert mount in source
    assert source.count("if deferred_features_enabled(active_feature_profile):") >= 3


def test_mixed_routers_gate_deferred_operations():
    routes_dir = Path(__file__).parents[1] / "app" / "routes"
    portfolio = (routes_dir / "portfolio.py").read_text(encoding="utf-8")
    subscriptions = (routes_dir / "subscriptions.py").read_text(encoding="utf-8")

    assert '@router.get("")' in portfolio
    assert '@router.get("", dependencies=' not in portfolio
    assert portfolio.count("dependencies=[Depends(require_deferred_features)]") == 14
    assert subscriptions.count("dependencies=[Depends(require_deferred_features)]") == 4
