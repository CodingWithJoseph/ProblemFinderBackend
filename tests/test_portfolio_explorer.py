"""The multi-metric explorer: one row per page with loads, error rate,
felt-speed (LCP p75 + rating), a health badge, and a loads sparkline."""

from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline, make_usage_source


async def test_get_explorer_builds_per_page_rows(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),                    # launched product lookup
        FakeResult(rows=[]),                           # app settings overlay
        FakeResult(rows=[source]),                     # usage source
        FakeResult(rows=[("/cart", 100), ("/home", 50)]),  # loads per url
        FakeResult(rows=[("/cart", 7)]),               # errors per url
        FakeResult(rows=[("/cart", 3200.0)]),          # LCP p75 per url
        FakeResult(rows=[]),                           # daily loads (spark)
    ])

    resp = await client.get(f"/portfolio/{product.id}/explorer", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    rows = {r["url"]: r for r in body["rows"]}

    cart = rows["/cart"]
    assert cart["loads"] == 100
    assert cart["errors"] == 7
    assert cart["errorRate"] == 0.07
    assert cart["lcpP75"] == 3200.0
    assert cart["lcpRating"] == "needs-improvement"   # 2500..4000
    assert cart["health"] == "unhealthy"              # errorRate >= 5%
    assert isinstance(cart["spark"], list)

    home = rows["/home"]
    assert home["errors"] == 0
    assert home["errorRate"] == 0.0
    assert home["lcpP75"] is None
    assert home["lcpRating"] is None
    assert home["health"] == "healthy"

    # Sparklines are window-aligned and equal length across rows.
    assert len(cart["spark"]) == len(home["spark"]) > 0


async def test_get_explorer_requires_launched_product(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.get("/portfolio/nope/explorer", headers=auth_headers)
    assert resp.status_code == 404
