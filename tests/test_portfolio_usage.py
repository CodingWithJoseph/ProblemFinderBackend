from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from conftest import (
    FakeResult,
    make_pipeline,
    make_revenue_source,
    make_usage_event,
    make_usage_source,
)
from app.routes import portfolio as portfolio_routes
from app.models import PortfolioRevenueSource, PortfolioUsageEvent, PortfolioUsageSource


async def test_portfolio_product_includes_usage_source(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # issue counts
        FakeResult(rows=[source]),   # usage source
        FakeResult(rows=[]),         # revenue source
    ])

    resp = await client.get(f"/portfolio/{product.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["id"] == product.id
    assert body["usageSource"]["publicKey"] == source.public_key
    assert "productUrl" in body["usageSource"]
    assert "allowedDomain" in body["usageSource"]
    assert body["revenueSource"] is None


async def test_create_usage_source_for_launched_product(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # no existing source
    ])

    resp = await client.post(f"/portfolio/{product.id}/usage-source", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["pipelineId"] == product.id
    assert body["publicKey"]
    assert any(isinstance(obj, PortfolioUsageSource) and obj.pipeline_id == product.id for obj in fake_db.added)


async def test_update_usage_source_setup_metadata(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[source]),   # usage source
    ])

    resp = await client.patch(
        f"/portfolio/{product.id}/usage-source",
        headers=auth_headers,
        json={"product_url": "https://www.example.com/launch", "allowed_domain": "example.com"},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["productUrl"] == "https://www.example.com/launch"
    assert body["allowedDomain"] == "example.com"
    assert source.product_url == "https://www.example.com/launch"
    assert source.allowed_domain == "example.com"
    assert fake_db.commit_count == 1


async def test_get_usage_metrics(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    event = make_usage_event(pipeline_id=product.id, source_id=source.id)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),                 # launched product lookup
            FakeResult(rows=[source]),                  # usage source
            FakeResult(rows=[("pageview", 4), ("signup", 1)]),
            FakeResult(rows=[("2026-05-01", "pageview", 4, 2), ("2026-05-01", "signup", 1, 1)]),
            FakeResult(rows=[event]),
        ],
        scalar=[5, 2, 2],
    )

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["connected"] is True
    assert body["totalEvents"] == 5
    assert body["summary14d"]["pageviews"] == 4
    assert body["summary14d"]["signups"] == 1
    assert body["summary14d"]["visitors"] == 2
    assert body["recentEvents"][0]["eventType"] == "pageview"


async def test_create_revenue_source_for_launched_product(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # no existing revenue source
    ])

    resp = await client.post(
        f"/portfolio/{product.id}/revenue-source",
        headers=auth_headers,
        json={"provider": "stripe"},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["pipelineId"] == product.id
    assert body["provider"] == "stripe"
    assert body["status"] == "not_connected"
    assert any(isinstance(obj, PortfolioRevenueSource) and obj.pipeline_id == product.id for obj in fake_db.added)


async def test_create_revenue_connect_url_stores_oauth_state(client, fake_db, auth_headers, monkeypatch):
    monkeypatch.setattr(portfolio_routes.settings, "stripe_connect_client_id", "ca_test_123")
    monkeypatch.setattr(portfolio_routes.settings, "app_url", "https://console.example")
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_revenue_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[source]),   # existing revenue source
    ])

    resp = await client.post(
        f"/portfolio/{product.id}/revenue-source/connect",
        headers=auth_headers,
        json={"provider": "stripe"},
    )

    assert resp.status_code == 200
    url = resp.json()["data"]["url"]
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "connect.stripe.com"
    assert params["client_id"] == ["ca_test_123"]
    assert params["redirect_uri"] == ["https://console.example/api/portfolio/stripe/callback"]
    assert params["state"] == [source.oauth_state]
    assert source.oauth_state
    assert source.oauth_state_expires_at is not None
    assert fake_db.commit_count == 1


async def test_complete_revenue_connect_marks_source_connected(client, fake_db, monkeypatch):
    monkeypatch.setattr(portfolio_routes.settings, "app_url", "https://console.example")
    monkeypatch.setattr(
        portfolio_routes.stripe.OAuth,
        "token",
        lambda **kwargs: {"stripe_user_id": "acct_123"},
    )
    source = make_revenue_source(
        pipeline_id="pipe-1",
        oauth_state="state-123",
        oauth_state_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.get("/portfolio/revenue-source/stripe/callback?code=code-123&state=state-123")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["redirectUrl"] == "https://console.example/dashboard/portfolio?pipelineId=pipe-1&stripe=connected"
    assert source.status == "connected"
    assert source.provider_account_id == "acct_123"
    assert source.provider_account_label == "acct_123"
    assert source.oauth_state is None
    assert source.oauth_state_expires_at is None
    assert source.connected_at is not None


async def test_get_revenue_metrics_placeholder(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_revenue_source(
        pipeline_id=product.id,
        status="connected",
        current_mrr_cents=12000,
        new_customers_30d=3,
        churned_customers_30d=1,
        churn_rate_30d=0.05,
    )
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[source]),   # revenue source
    ])

    resp = await client.get(f"/portfolio/{product.id}/revenue", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["connected"] is True
    assert body["source"]["provider"] == "stripe"
    assert body["summary"]["mrrCents"] == 12000
    assert body["summary"]["newCustomers30d"] == 3
    assert body["summary"]["churnedCustomers30d"] == 1
    assert body["summary"]["churnRate30d"] == 0.05


async def test_public_usage_event_ingestion(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/events",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "event_type": "signup",
            "visitor_id": "visitor-1",
            "session_id": "session-1",
            "url": "https://example.com/signup",
            "metadata": {"plan": "pro"},
        },
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    event = next(obj for obj in fake_db.added if isinstance(obj, PortfolioUsageEvent))
    assert event.pipeline_id == source.pipeline_id
    assert event.event_type == "signup"
    assert event.event_metadata == {"plan": "pro"}
    assert source.last_seen_at is not None


async def test_public_usage_event_rejects_unapproved_domain(client, fake_db):
    source = make_usage_source(allowed_domain="example.com")
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/events",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "event_type": "pageview",
            "visitor_id": "visitor-1",
            "url": "https://not-example.test/",
        },
    )

    assert resp.status_code == 403
