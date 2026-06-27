from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline
from app.models import MonitorAlertSettings


async def test_get_alert_settings_returns_defaults(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # no settings row -> defaults
    ])

    resp = await client.get(f"/portfolio/{product.id}/alert-settings", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["newIssueEnabled"] is True
    assert body["errorSpikeEnabled"] is True
    assert body["errorSpikeMultiplier"] == 3.0   # global default
    assert body["signupsDropPct"] == 0.5
    assert body["revenueDropPct"] == 0.2


async def test_update_alert_settings_creates_and_overrides(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # no existing settings row
    ])

    resp = await client.put(
        f"/portfolio/{product.id}/alert-settings",
        headers=auth_headers,
        json={"error_spike_enabled": False, "signups_drop_pct": 0.7},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["errorSpikeEnabled"] is False
    assert body["signupsDropPct"] == 0.7
    assert body["newIssueEnabled"] is True       # untouched -> default on
    assert body["revenueDropPct"] == 0.2         # untouched -> global default
    assert any(isinstance(o, MonitorAlertSettings) and o.pipeline_id == product.id for o in fake_db.added)
    assert fake_db.commit_count == 1


async def test_update_alert_settings_rejects_out_of_range(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product])])

    resp = await client.put(
        f"/portfolio/{product.id}/alert-settings",
        headers=auth_headers,
        json={"signups_drop_pct": 2.0},  # > 1.0
    )

    assert resp.status_code == 422
