"""Over-time hero chart: daily points, the normal-range baseline band, and
deploy markers."""

from datetime import datetime, timedelta, timezone

from conftest import FakeResult, make_pipeline
from app.services.monitoring.analytics import _series_stats


def test_series_stats_band():
    stats = _series_stats([0, 0, 10, 0, 0])  # mean 2, std 4
    assert stats["mean"] == 2.0
    assert stats["lower"] == 0.0          # clamped at zero
    assert stats["upper"] == 6.0


def test_series_stats_empty():
    assert _series_stats([]) == {"mean": 0.0, "lower": 0.0, "upper": 0.0}


async def test_get_timeseries_points_baseline_and_markers(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),                       # launched product lookup
        FakeResult(rows=[]),                              # app settings overlay
        FakeResult(rows=[(today, 10), (yesterday, 4)]),   # daily series
        FakeResult(rows=[("v1.0", yesterday), ("v1.1", today)]),  # release first-seen
    ])

    resp = await client.get(f"/portfolio/{product.id}/timeseries?metric=errors", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["metric"] == "errors"
    # Series is zero-filled across the window; only our two days carry counts.
    assert len(body["points"]) == body["windowDays"]
    assert sum(p["value"] for p in body["points"]) == 14
    assert body["baseline"]["mean"] == round(14 / body["windowDays"], 2)
    # Deploy markers ordered by date.
    assert body["markers"] == [
        {"date": yesterday.isoformat(), "release": "v1.0"},
        {"date": today.isoformat(), "release": "v1.1"},
    ]


async def test_get_timeseries_rejects_unknown_metric(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product])])
    resp = await client.get(f"/portfolio/{product.id}/timeseries?metric=bogus", headers=auth_headers)
    assert resp.status_code == 422  # Literal validation
