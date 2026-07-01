from datetime import date, datetime, timezone

from conftest import FakeResult, make_revenue_source
from app.models import MonitorCustomerMrr
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
    previous_month_mrr = MonitorCustomerMrr(
        id="mrr-previous-month",
        revenue_source_id=source.id,
        stripe_customer_id="cus_1",
        as_of_date=date(2026, 5, 30),
        mrr_cents=1000,
        currency="usd",
    )
    prior_mrr = MonitorCustomerMrr(
        id="mrr-prior",
        revenue_source_id=source.id,
        stripe_customer_id="cus_1",
        as_of_date=date(2026, 6, 23),
        mrr_cents=1200,
        currency="usd",
    )
    current_mrr = MonitorCustomerMrr(
        id="mrr-current",
        revenue_source_id=source.id,
        stripe_customer_id="cus_1",
        as_of_date=date(2026, 6, 30),
        mrr_cents=1500,
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
        FakeResult(rows=[previous_month_mrr, prior_mrr, current_mrr]),
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

    assert metrics["revenue"]["unit"] == "cents"
    assert metrics["revenue"]["currentTotal"] == 1500
    assert metrics["revenue"]["priorTotal"] == 1000
    assert metrics["revenue"]["percentChange"] == 0.5
    assert metrics["revenue"]["comparisonDate"] == "2026-05-30"

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
