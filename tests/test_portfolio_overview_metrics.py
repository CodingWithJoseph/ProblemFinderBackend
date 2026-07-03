from datetime import date, datetime, timezone

from conftest import FakeResult, make_revenue_source
from app.models import MonitorRevenueDaily
from app.routes import portfolio as portfolio_routes


def test_previous_month_comparison_clamps_month_end():
    assert portfolio_routes._previous_month_same_day(date(2026, 3, 31)) == date(2026, 2, 28)
    assert portfolio_routes._previous_month_same_day(date(2024, 3, 31)) == date(2024, 2, 29)


async def test_portfolio_overview_metrics_returns_fixed_comparison_and_sparkline(
    client, fake_db, auth_headers, monkeypatch
):
    monkeypatch.setattr(
        portfolio_routes,
        "_now",
        lambda: datetime(2026, 6, 30, 12, tzinfo=timezone.utc),
    )
    source = make_revenue_source(pipeline_id="pipeline-1")
    # Daily gross revenue rows: two land in the trailing 7 days (2026-06-24..30)
    # and one in the prior 7 days (2026-06-17..23). Missing days read as 0.
    prior_day = MonitorRevenueDaily(
        id="rev-prior",
        revenue_source_id=source.id,
        as_of_date=date(2026, 6, 19),
        gross_cents=1000,
        currency="usd",
    )
    trailing_day = MonitorRevenueDaily(
        id="rev-trailing",
        revenue_source_id=source.id,
        as_of_date=date(2026, 6, 26),
        gross_cents=500,
        currency="usd",
    )
    current_day = MonitorRevenueDaily(
        id="rev-current",
        revenue_source_id=source.id,
        as_of_date=date(2026, 6, 30),
        gross_cents=1500,
        currency="usd",
    )
    fake_db.stub(execute=[
        FakeResult(rows=[("pipeline-1",)]),
        FakeResult(rows=[("pageview", 100), ("custom", 50)]),
        FakeResult(rows=[
            (date(2026, 6, 23), "pageview", 5),
            (date(2026, 6, 23), "custom", 2),
            (date(2026, 6, 30), "pageview", 10),
            (date(2026, 6, 30), "custom", 4),
        ]),
        FakeResult(rows=[
            (date(2026, 6, 23), 8),
            (date(2026, 6, 30), 2),
        ]),
        FakeResult(scalar=3),
        FakeResult(rows=[]),  # effective app settings overlay
        FakeResult(rows=[source]),
        FakeResult(rows=[prior_day, trailing_day, current_day]),
    ])

    response = await client.get("/portfolio/overview-metrics", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["comparisonDays"] == 7
    assert body["sparklineDays"] == 14
    metrics = {metric["metric"]: metric for metric in body["metrics"]}

    assert metrics["traffic"]["currentTotal"] == 100
    assert metrics["traffic"]["comparisonCurrent"] == 10
    assert metrics["traffic"]["priorTotal"] == 5
    assert metrics["traffic"]["percentChange"] == 1.0
    assert metrics["traffic"]["trendDirection"] == "up"
    assert metrics["traffic"]["isPositiveTrend"] is True

    assert metrics["usage"]["currentTotal"] == 150
    assert metrics["usage"]["comparisonCurrent"] == 14
    assert metrics["usage"]["priorTotal"] == 7
    assert metrics["errors"]["currentTotal"] == 3
    assert metrics["errors"]["comparisonCurrent"] == 2
    assert metrics["errors"]["priorTotal"] == 8
    assert metrics["errors"]["trendDirection"] == "down"
    assert metrics["errors"]["isPositiveTrend"] is True

    # Revenue is now daily gross with a trailing-7 vs prior-7 comparison, like
    # the other cards: trailing = 1500 + 500 = 2000, prior = 1000.
    assert metrics["revenue"]["unit"] == "cents"
    assert metrics["revenue"]["currentTotal"] == 2000
    assert metrics["revenue"]["comparisonCurrent"] == 2000
    assert metrics["revenue"]["priorTotal"] == 1000
    assert metrics["revenue"]["percentChange"] == 1.0
    assert metrics["revenue"]["trendDirection"] == "up"
    assert metrics["revenue"]["isPositiveTrend"] is True
    # The month-over-month MRR comparison is gone.
    assert metrics["revenue"]["comparisonDate"] is None
    # Days with charges carry their gross; every other day is zero-filled so the
    # sparkline is a continuous daily line.
    revenue_points = {point["date"]: point["value"] for point in metrics["revenue"]["points"]}
    assert revenue_points["2026-06-30"] == 1500
    assert revenue_points["2026-06-26"] == 500
    assert revenue_points["2026-06-19"] == 1000
    assert revenue_points["2026-06-17"] == 0

    for metric in metrics.values():
        assert len(metric["points"]) == 14
        assert metric["points"][0]["date"] == "2026-06-17"
        assert metric["points"][-1]["date"] == "2026-06-30"


async def test_portfolio_overview_metrics_is_zero_filled_without_products(
    client, fake_db, auth_headers, monkeypatch
):
    monkeypatch.setattr(
        portfolio_routes,
        "_now",
        lambda: datetime(2026, 6, 30, 12, tzinfo=timezone.utc),
    )
    fake_db.stub(execute=[FakeResult(rows=[])])

    response = await client.get("/portfolio/overview-metrics", headers=auth_headers)

    assert response.status_code == 200
    metrics = {metric["metric"]: metric for metric in response.json()["data"]["metrics"]}
    assert metrics["traffic"]["currentTotal"] == 0
    assert metrics["usage"]["currentTotal"] == 0
    assert metrics["errors"]["currentTotal"] == 0
    assert metrics["revenue"]["currentTotal"] is None
    assert all(point["value"] == 0 for point in metrics["traffic"]["points"])
    assert all(point["value"] is None for point in metrics["revenue"]["points"])


def _charge(*, amount, currency, when, status="succeeded"):
    return {
        "amount": amount,
        "currency": currency,
        "status": status,
        "created": int(when.timestamp()),
    }


def test_daily_gross_by_day_currency_sums_succeeded_charges_by_day():
    charges = [
        _charge(amount=1000, currency="usd", when=datetime(2026, 6, 30, 9, tzinfo=timezone.utc)),
        _charge(amount=500, currency="usd", when=datetime(2026, 6, 30, 20, tzinfo=timezone.utc)),
        _charge(amount=2000, currency="eur", when=datetime(2026, 6, 29, 1, tzinfo=timezone.utc)),
        # Excluded: not succeeded, and zero-amount.
        _charge(amount=999, currency="usd", when=datetime(2026, 6, 30, 3, tzinfo=timezone.utc), status="failed"),
        _charge(amount=0, currency="usd", when=datetime(2026, 6, 30, 4, tzinfo=timezone.utc)),
    ]

    totals = portfolio_routes._daily_gross_by_day_currency(charges)

    assert totals == {
        (date(2026, 6, 30), "usd"): 1500,
        (date(2026, 6, 29), "eur"): 2000,
    }


def test_portfolio_daily_revenue_values_zero_fills_and_converts_currency():
    dates = [date(2026, 6, 28), date(2026, 6, 29), date(2026, 6, 30)]
    rows = [
        MonitorRevenueDaily(id="a", revenue_source_id="s", as_of_date=date(2026, 6, 28), gross_cents=1000, currency="usd"),
        MonitorRevenueDaily(id="b", revenue_source_id="s", as_of_date=date(2026, 6, 30), gross_cents=2000, currency="eur"),
    ]

    values = portfolio_routes._portfolio_daily_revenue_values(rows, dates, "usd", {"eur": 1.1})

    # 06-28 as-is, 06-29 zero-filled, 06-30 converted 2000 * 1.1 = 2200.
    assert values == [1000, 0, 2200]
