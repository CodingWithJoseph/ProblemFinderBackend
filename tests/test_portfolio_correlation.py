from datetime import date, datetime, timezone

from conftest import FakeResult, make_pipeline
from app.services.monitoring.analytics import _build_correlation_days, _correlation_insights


def test_build_correlation_days_merges_signals():
    today = date(2026, 6, 1)
    usage_rows = [
        (date(2026, 5, 31), "pageview", 100, 40),
        (date(2026, 5, 31), "signup", 5, 5),
        (date(2026, 6, 1), "pageview", 10, 3),
    ]
    error_rows = [(date(2026, 5, 31), 50)]

    days = _build_correlation_days(usage_rows, error_rows, days=30, today=today)

    assert len(days) == 30
    by_date = {d["date"]: d for d in days}
    may31 = by_date["2026-05-31"]
    assert may31["pageviews"] == 100
    assert may31["visitors"] == 40
    assert may31["signups"] == 5
    assert may31["errors"] == 50
    assert by_date["2026-06-01"]["pageviews"] == 10


def test_correlation_insights_flags_error_spike_with_signup_dip():
    days = [{"date": "2026-05-30", "visitors": 0, "pageviews": 0, "signups": 0, "errors": 0} for _ in range(29)]
    days.append({"date": "2026-05-31", "visitors": 40, "pageviews": 100, "signups": 0, "errors": 50})

    insights = _correlation_insights(days)

    assert insights
    assert "2026-05-31" in insights[0]
    assert "signups" in insights[0]


def test_correlation_insights_quiet_when_flat():
    days = [{"date": f"2026-05-{i:02d}", "visitors": 5, "pageviews": 10, "signups": 1, "errors": 1} for i in range(1, 15)]
    assert _correlation_insights(days) == []


async def test_get_correlation_endpoint(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),  # launched product lookup
            FakeResult(rows=[]),         # app settings overlay
            FakeResult(rows=[(date(2026, 5, 31), "signup", 5, 5)]),  # usage daily
            FakeResult(rows=[(date(2026, 5, 31), 50)]),              # error daily
        ],
        scalar=[
            None,  # revenue source (none connected)
            4,     # this_week_signups
            10,    # prev_week_signups
            60,    # errors_14d
            120,   # sessions_14d
        ],
    )

    resp = await client.get(f"/portfolio/{product.id}/correlation", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert len(body["days"]) == 30
    assert body["windowDays"] == 30
    assert body["summary"]["mrrCents"] is None
    assert body["summary"]["signupsChangePct"] == -0.6   # (4 - 10) / 10
    assert body["summary"]["errorsPerSession14d"] == 0.5  # 60 / 120


async def test_error_correlation_alias(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),
            FakeResult(rows=[]),
            FakeResult(rows=[(date(2026, 5, 31), "signup", 5, 5)]),
            FakeResult(rows=[(date(2026, 5, 31), 50)]),
        ],
        scalar=[None, 4, 10, 60, 120],
    )

    # The /insights/error-correlation alias serves the same payload as /correlation.
    resp = await client.get(f"/portfolio/{product.id}/insights/error-correlation", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["windowDays"] == 30
