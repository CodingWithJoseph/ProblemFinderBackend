from conftest import (
    FakeResult,
    make_pipeline,
    make_subscription,
    make_usage_source,
)
from datetime import datetime, timezone

from app.models import AppSetting, PlanEnum


# ── admin settings endpoint ──────────────────────────────────────────────────

async def test_get_admin_settings_lists_knobs_with_defaults(client, fake_db, auth_headers):
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],  # admin gate
        execute=[FakeResult(rows=[])],                    # no overrides saved
    )

    resp = await client.get("/portfolio/admin/settings", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()["data"]
    by_key = {d["key"]: d for d in data}
    # A choice knob exposes its option set; value is the config default.
    usage = by_key["analytics_usage_window_days"]
    assert usage["kind"] == "choice"
    assert usage["options"] == [7, 14, 30]
    assert usage["value"] == 14
    assert usage["isDefault"] is True
    # A toggle is surfaced as a boolean.
    alerts = by_key["alerts_enabled"]
    assert alerts["kind"] == "toggle"
    assert alerts["value"] is True


async def test_get_admin_settings_reflects_saved_override(client, fake_db, auth_headers):
    override = AppSetting(key="analytics_usage_window_days", value=7, updated_at=datetime.now(timezone.utc))
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],
        execute=[FakeResult(rows=[override])],
    )

    resp = await client.get("/portfolio/admin/settings", headers=auth_headers)

    assert resp.status_code == 200
    usage = next(d for d in resp.json()["data"] if d["key"] == "analytics_usage_window_days")
    assert usage["value"] == 7
    assert usage["isDefault"] is False


async def test_admin_settings_forbidden_for_non_admin(client, fake_db, auth_headers):
    fake_db.stub(scalar=[make_subscription(plan=PlanEnum.pro)])
    resp = await client.get("/portfolio/admin/settings", headers=auth_headers)
    assert resp.status_code == 403


async def test_update_admin_settings_persists_and_returns(client, fake_db, auth_headers):
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],
        execute=[
            FakeResult(rows=[]),  # existing lookup for key 1 -> none, insert
            FakeResult(rows=[]),  # existing lookup for key 2 -> none, insert
            FakeResult(rows=[    # final re-serialize reads saved rows
                AppSetting(key="analytics_usage_window_days", value=30),
                AppSetting(key="alerts_enabled", value=False),
            ]),
        ],
    )

    resp = await client.put(
        "/portfolio/admin/settings",
        headers=auth_headers,
        json={"updates": {"analytics_usage_window_days": 30, "alerts_enabled": False}},
    )

    assert resp.status_code == 200
    added = {o.key for o in fake_db.added if isinstance(o, AppSetting)}
    assert added == {"analytics_usage_window_days", "alerts_enabled"}
    assert fake_db.commit_count >= 1
    by_key = {d["key"]: d for d in resp.json()["data"]}
    assert by_key["analytics_usage_window_days"]["value"] == 30
    assert by_key["analytics_usage_window_days"]["isDefault"] is False
    assert by_key["alerts_enabled"]["value"] is False


async def test_update_admin_settings_rejects_out_of_range_value(client, fake_db, auth_headers):
    fake_db.stub(scalar=[make_subscription(plan=PlanEnum.admin)])
    resp = await client.put(
        "/portfolio/admin/settings",
        headers=auth_headers,
        json={"updates": {"analytics_usage_window_days": 99}},  # not an allowed option
    )
    assert resp.status_code == 400
    assert not any(isinstance(o, AppSetting) for o in fake_db.added)


async def test_update_admin_settings_rejects_unknown_key(client, fake_db, auth_headers):
    fake_db.stub(scalar=[make_subscription(plan=PlanEnum.admin)])
    resp = await client.put(
        "/portfolio/admin/settings",
        headers=auth_headers,
        json={"updates": {"not_a_real_setting": 5}},
    )
    assert resp.status_code == 400


async def test_update_admin_settings_forbidden_for_non_admin(client, fake_db, auth_headers):
    fake_db.stub(scalar=[make_subscription(plan=PlanEnum.free)])
    resp = await client.put(
        "/portfolio/admin/settings",
        headers=auth_headers,
        json={"updates": {"analytics_usage_window_days": 7}},
    )
    assert resp.status_code == 403


# ── overlay actually drives a read endpoint ──────────────────────────────────

async def test_usage_window_respects_admin_override(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    override = AppSetting(key="analytics_usage_window_days", value=30)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),    # launched product lookup
            FakeResult(rows=[override]),   # app settings overlay -> usage window 30
            FakeResult(rows=[source]),     # usage source
            FakeResult(rows=[]),           # window counts
            FakeResult(rows=[]),           # daily
            FakeResult(rows=[]),           # recent events
            FakeResult(rows=[]),           # funnel
            FakeResult(rows=[]),           # retention first-seen
            FakeResult(rows=[]),           # retention active-days
        ],
        scalar=[0, 0, 0, 0, 0, 0, 0],
    )

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["windowDays"] == 30
