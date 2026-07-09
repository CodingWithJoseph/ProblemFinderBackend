from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import (
    FakeResult,
    make_error_event,
    make_error_group,
    make_pipeline,
    make_revenue_source,
    make_subscription,
    make_usage_event,
    make_usage_source,
)
from app.routes import portfolio as portfolio_routes
from app.services.monitoring import ingest as monitor_ingest
from app.services.monitoring.analytics import _compute_retention
from app.models import (
    Pipeline,
    PlanEnum,
    MonitorRevenueSource,
    MonitorUsageEvent,
    MonitorUsageSource,
)


def test_compute_retention_d1_d7():
    today = date(2026, 6, 1)
    first_seen = [("a", "2026-05-20"), ("b", "2026-05-20"), ("c", "2026-05-31")]
    active = [
        ("a", "2026-05-20"), ("a", "2026-05-21"), ("a", "2026-05-27"),  # D1 + D7 returns
        ("b", "2026-05-20"),                                            # no return
        ("c", "2026-05-31"), ("c", "2026-06-01"),                       # D1 return only
    ]

    r = _compute_retention(first_seen, active, today)

    # D1 eligible: a, b, c (all observed for >=1 day); retained: a, c
    assert r["d1"]["eligible"] == 3
    assert r["d1"]["retained"] == 2
    assert r["d1"]["rate"] == round(2 / 3, 4)
    # D7 eligible: a, b (5/20 + 7 <= today); c is too new; retained: a
    assert r["d7"]["eligible"] == 2
    assert r["d7"]["retained"] == 1
    assert r["d7"]["rate"] == 0.5
    # Cohorts, most recent first
    assert [c["date"] for c in r["cohorts"]] == ["2026-05-31", "2026-05-20"]
    newest = r["cohorts"][0]
    assert newest["size"] == 1 and newest["d1Rate"] == 1.0 and newest["d7Rate"] is None


class StripeList:
    def __init__(self, data):
        self.data = data

    def auto_paging_iter(self):
        return iter(self.data)


async def test_portfolio_product_includes_usage_source(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # app settings overlay (revenue engine flag)
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
    assert any(isinstance(obj, MonitorUsageSource) and obj.pipeline_id == product.id for obj in fake_db.added)


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
            FakeResult(rows=[]),                        # app settings overlay
            FakeResult(rows=[source]),                  # usage source
            FakeResult(rows=[("pageview", 4), ("signup", 1)]),
            FakeResult(rows=[("2026-05-01", "pageview", 4, 2), ("2026-05-01", "signup", 1, 1)]),
            FakeResult(rows=[event]),                   # recent events (secondary)
            FakeResult(rows=[                           # top pages rollup
                ("https://example.com/", 4, 2, datetime(2026, 5, 1, tzinfo=timezone.utc)),
            ]),
            FakeResult(rows=[                           # top events rollup
                ("signup", 1, 1, datetime(2026, 5, 1, tzinfo=timezone.utc)),
            ]),
        ],
        scalar=[5, 2, 2],
    )

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["connected"] is True
    assert body["totalEvents"] == 5
    # Window is echoed from config so the frontend renders labels from data.
    assert body["windowDays"] == 14
    assert body["growthWindowDays"] == 7
    assert body["summary14d"]["pageviews"] == 4
    assert body["summary14d"]["signups"] == 1
    assert body["summary14d"]["visitors"] == 2
    # Lead-with rollups (aggregation engine v0).
    assert body["topPages"][0] == {
        "url": "https://example.com/", "views": 4, "visitors": 2,
        "lastSeenAt": "2026-05-01T00:00:00+00:00",
    }
    assert body["topEvents"][0]["name"] == "signup"
    assert body["topEvents"][0]["count"] == 1
    assert body["recentEvents"][0]["eventType"] == "pageview"


async def test_launched_product_without_source_is_not_connected(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # app settings overlay
        FakeResult(rows=[]),         # no project-scoped usage source
    ])

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["source"] is None
    assert body["connected"] is False
    assert body["totalEvents"] == 0
    assert body["health"]["state"] == "no-data"
    assert body["summary14d"]["visitors"] == 0


async def test_project_source_without_events_is_connected_but_has_no_data(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id, last_seen_at=None)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # app settings overlay
        FakeResult(rows=[source]),   # this product's configured source
    ])

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["source"]["pipelineId"] == product.id
    assert body["connected"] is True
    assert body["totalEvents"] == 0
    assert body["health"]["state"] == "no-data"


def test_usage_connection_requires_active_project_scoped_key():
    from app.routes.portfolio import _usage_source_is_connected

    project_a = "11111111-1111-1111-1111-111111111111"
    project_b = "22222222-2222-2222-2222-222222222222"
    source = make_usage_source(pipeline_id=project_a)

    assert _usage_source_is_connected(source, project_a) is True
    assert _usage_source_is_connected(source, project_b) is False
    assert _usage_source_is_connected(make_usage_source(pipeline_id=project_a, status="paused"), project_a) is False
    assert _usage_source_is_connected(make_usage_source(pipeline_id=project_a, public_key=""), project_a) is False
    assert _usage_source_is_connected(None, project_a) is False


async def test_get_usage_metrics_funnel_and_growth(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),   # launched product lookup
            FakeResult(rows=[]),          # app settings overlay
            FakeResult(rows=[source]),    # usage source
            FakeResult(rows=[("pageview", 100), ("signup", 20), ("activation", 8)]),  # window counts
            FakeResult(rows=[]),          # daily
            FakeResult(rows=[]),          # recent events
            FakeResult(rows=[]),          # top pages rollup
            FakeResult(rows=[]),          # top events rollup
            FakeResult(rows=[("signup", 20), ("activation", 8)]),  # funnel: distinct visitors per stage
            FakeResult(rows=[]),          # retention: first-seen per visitor
            FakeResult(rows=[]),          # retention: distinct visitor active-days
        ],
        scalar=[
            128,  # total_events
            100,  # visitors_14d
            90,   # active_users_14d
            60,   # this_week_visitors
            40,   # prev_week_visitors
            12,   # this_week_signups
            8,    # prev_week_signups
        ],
    )

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]

    funnel = body["funnel"]
    assert funnel["visited"] == 100
    assert funnel["signedUp"] == 20
    assert funnel["activated"] == 8
    assert funnel["signupRate"] == 0.2      # 20 / 100
    assert funnel["activationRate"] == 0.4  # 8 / 20

    growth = body["growth"]
    assert growth["visitors"]["current"] == 60
    assert growth["visitors"]["previous"] == 40
    assert growth["visitors"]["changePct"] == 0.5  # (60 - 40) / 40
    assert growth["signups"]["changePct"] == 0.5   # (12 - 8) / 8

    retention = body["retention"]
    assert retention["windowDays"] == 30
    assert retention["d1"]["rate"] is None  # no retention rows stubbed
    assert retention["cohorts"] == []


async def test_admin_can_create_monitored_product(client, fake_db, auth_headers):
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],  # admin gate
        execute=[FakeResult(rows=[])],                    # issue counts for the new card
    )

    resp = await client.post(
        "/portfolio/products",
        headers=auth_headers,
        json={"name": "Immensity", "product_url": "https://www.useimmensity.com/app"},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["name"] == "Immensity"
    assert body["launchedAt"] is not None
    # Usage source is auto-created and live, with the domain derived from the URL.
    assert body["usageSource"]["publicKey"]
    assert body["usageSource"]["allowedDomain"] == "useimmensity.com"
    assert body["revenueSource"] is None
    # A launched Pipeline and its usage source were persisted together.
    product = next(o for o in fake_db.added if isinstance(o, Pipeline))
    assert product.launched_at is not None
    assert any(isinstance(o, MonitorUsageSource) and o.pipeline_id == product.id for o in fake_db.added)


async def test_non_admin_cannot_create_monitored_product(client, fake_db, auth_headers):
    fake_db.stub(scalar=[make_subscription(plan=PlanEnum.pro)])

    resp = await client.post(
        "/portfolio/products",
        headers=auth_headers,
        json={"name": "X", "product_url": "https://x.com"},
    )

    assert resp.status_code == 403
    assert not any(isinstance(o, Pipeline) for o in fake_db.added)


async def test_create_monitored_product_requires_subscription(client, fake_db, auth_headers):
    fake_db.stub(scalar=[None])  # no subscription row at all

    resp = await client.post(
        "/portfolio/products",
        headers=auth_headers,
        json={"name": "X", "product_url": "https://x.com"},
    )

    assert resp.status_code == 403


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
    assert any(isinstance(obj, MonitorRevenueSource) and obj.pipeline_id == product.id for obj in fake_db.added)


async def test_create_revenue_connect_url_stores_oauth_state(client, fake_db, auth_headers, monkeypatch):
    monkeypatch.setattr(portfolio_routes.settings, "stripe_connect_client_id", "ca_test_123")
    monkeypatch.setattr(portfolio_routes.settings, "app_url", "https://console.example")
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_revenue_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # app settings overlay
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
    assert body["redirectUrl"] == "https://console.example/dashboard/monitor/setup?pipelineId=pipe-1&stripe=connected"
    assert source.status == "connected"
    assert source.provider_account_id == "acct_123"
    assert source.provider_account_label == "acct_123"
    assert source.oauth_state is None
    assert source.oauth_state_expires_at is None
    assert source.connected_at is not None


async def test_usage_window_is_config_driven(client, fake_db, auth_headers, monkeypatch):
    # Changing the configured window flows straight through to the response.
    monkeypatch.setattr(portfolio_routes.settings, "analytics_usage_window_days", 7)
    monkeypatch.setattr(portfolio_routes.settings, "analytics_growth_window_days", 3)
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),
            FakeResult(rows=[]),  # app settings overlay
            FakeResult(rows=[source]),
            FakeResult(rows=[]),  # window counts
            FakeResult(rows=[]),  # daily
            FakeResult(rows=[]),  # recent events
            FakeResult(rows=[]),  # funnel
            FakeResult(rows=[]),  # retention first-seen
            FakeResult(rows=[]),  # retention active-days
        ],
        scalar=[0, 0, 0, 0, 0, 0, 0],
    )

    resp = await client.get(f"/portfolio/{product.id}/usage", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["windowDays"] == 7
    assert body["growthWindowDays"] == 3


async def test_get_revenue_metrics_placeholder(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_revenue_source(
        pipeline_id=product.id,
        status="connected",
        provider_account_id="acct_123",
        current_mrr_cents=12000,
        new_customers_30d=3,
        churned_customers_30d=1,
        churn_rate_30d=0.05,
    )
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # app settings overlay
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


async def test_sync_revenue_metrics_from_connected_stripe_account(client, fake_db, auth_headers, monkeypatch):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_revenue_source(
        pipeline_id=product.id,
        status="connected",
        provider_account_id="acct_123",
    )
    now = int(datetime.now(timezone.utc).timestamp())

    monkeypatch.setattr(
        portfolio_routes.stripe.Subscription,
        "list",
        lambda **kwargs: StripeList([
            {
                "id": "sub_active",
                "status": "active",
                "items": {
                    "data": [
                        {
                            "quantity": 2,
                            "price": {
                                "unit_amount": 5000,
                                "recurring": {"interval": "month", "interval_count": 1},
                            },
                        }
                    ]
                },
            }
        ]) if kwargs["status"] == "active" else StripeList([
            {"id": "sub_canceled", "status": "canceled", "canceled_at": now},
            {"id": "sub_old", "status": "canceled", "canceled_at": now - 60 * 24 * 60 * 60},
        ]),
    )
    monkeypatch.setattr(
        portfolio_routes.stripe.Customer,
        "list",
        lambda **kwargs: StripeList([{"id": "cus_1"}, {"id": "cus_2"}]),
    )
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # app settings overlay
        FakeResult(rows=[source]),   # revenue source
    ])

    resp = await client.post(f"/portfolio/{product.id}/revenue/sync", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["summary"]["mrrCents"] == 10000
    assert body["summary"]["newCustomers30d"] == 2
    assert body["summary"]["churnedCustomers30d"] == 1
    assert body["summary"]["churnRate30d"] == 0.5
    assert source.last_synced_at is not None
    assert source.revenue_snapshot["activeSubscriptions"] == 1
    assert fake_db.commit_count == 1


async def test_scheduled_sync_refreshes_connected_sources(fake_db, monkeypatch):
    source_a = make_revenue_source(status="connected", provider_account_id="acct_a")
    source_b = make_revenue_source(status="connected", provider_account_id="acct_b")
    fake_db.stub(execute=[
        FakeResult(rows=[]),  # app settings overlay
        FakeResult(rows=[source_a, source_b]),
    ])

    monkeypatch.setattr(
        portfolio_routes.stripe.Subscription,
        "list",
        lambda **kwargs: StripeList([]),
    )
    monkeypatch.setattr(
        portfolio_routes.stripe.Customer,
        "list",
        lambda **kwargs: StripeList([]),
    )

    synced = await portfolio_routes._sync_connected_revenue_sources(fake_db)

    assert synced == 2
    assert source_a.last_synced_at is not None
    assert source_b.last_synced_at is not None
    assert fake_db.commit_count == 2  # one commit per source


async def test_scheduled_sync_skips_failing_source(fake_db, monkeypatch):
    good = make_revenue_source(status="connected", provider_account_id="acct_good")
    bad = make_revenue_source(status="connected", provider_account_id="acct_bad")
    fake_db.stub(execute=[
        FakeResult(rows=[]),  # app settings overlay
        FakeResult(rows=[bad, good]),
    ])

    def _subscription_list(**kwargs):
        if kwargs.get("stripe_account") == "acct_bad":
            raise RuntimeError("stripe down")
        return StripeList([])

    monkeypatch.setattr(portfolio_routes.stripe.Subscription, "list", _subscription_list)
    monkeypatch.setattr(portfolio_routes.stripe.Customer, "list", lambda **kwargs: StripeList([]))

    synced = await portfolio_routes._sync_connected_revenue_sources(fake_db)

    # One source fails, the other still syncs.
    assert synced == 1
    assert good.last_synced_at is not None
    assert bad.last_synced_at is None


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
    event = next(obj for obj in fake_db.added if isinstance(obj, MonitorUsageEvent))
    assert event.pipeline_id == source.pipeline_id
    assert event.event_type == "signup"
    assert event.event_metadata == {"plan": "pro"}
    assert source.last_seen_at is not None


async def test_get_session_detail_timeline(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    t1 = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 20, 10, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc)
    view = make_usage_event(pipeline_id=product.id, session_id="sess-1", event_type="pageview", occurred_at=t1)
    signup = make_usage_event(pipeline_id=product.id, session_id="sess-1", event_type="signup", user_ref="user-9", occurred_at=t3)
    err = make_error_event(pipeline_id=product.id, session_id="sess-1", message="Boom", occurred_at=t2)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),          # launched product lookup
        FakeResult(rows=[source]),           # usage source
        FakeResult(rows=[view, signup]),     # usage events (scalars)
        FakeResult(rows=[err]),              # error events (scalars)
    ])

    resp = await client.get(f"/portfolio/{product.id}/sessions/sess-1", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["session"]["events"] == 2
    assert body["session"]["errors"] == 1
    assert body["session"]["identified"] is True
    assert body["session"]["durationSeconds"] == 300
    # Errors are merged into the usage timeline in chronological order.
    assert [item["kind"] for item in body["timeline"]] == ["event", "error", "event"]
    assert body["timeline"][1]["message"] == "Boom"


async def test_get_session_detail_404_when_empty(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),
        FakeResult(rows=[source]),
        FakeResult(rows=[]),   # no usage events
        FakeResult(rows=[]),   # no error events
    ])

    resp = await client.get(f"/portfolio/{product.id}/sessions/nope", headers=auth_headers)
    assert resp.status_code == 404


async def test_get_sessions_rollup(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    started = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),   # launched product lookup
            FakeResult(rows=[]),          # app settings overlay
            FakeResult(rows=[source]),    # usage source
            FakeResult(rows=[            # session rollup rows
                ("sess-1", "visitor-1", "user-9", started, ended, 7, 4),
                ("sess-2", "visitor-2", None, started, ended, 2, 2),
            ]),
        ],
        scalar=[2, 1, 9],  # total_sessions, identified_sessions, total_events
    )

    resp = await client.get(f"/portfolio/{product.id}/sessions", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["summary"]["totalSessions"] == 2
    assert body["summary"]["identifiedSessions"] == 1
    assert body["summary"]["avgEventsPerSession"] == 4.5  # 9 / 2
    first = body["sessions"][0]
    assert first["sessionId"] == "sess-1"
    assert first["userRef"] == "user-9"
    assert first["identified"] is True
    assert first["durationSeconds"] == 300  # 5 minutes
    assert first["events"] == 7
    assert first["pageviews"] == 4
    assert body["sessions"][1]["identified"] is False


async def test_ingest_vital(client, fake_db):
    from app.models import MonitorWebVital

    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/vitals",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "metric": "LCP",
            "value": 2300.0,
            "url": "https://example.com/",
            "session_id": "s1",
        },
    )

    assert resp.status_code == 200
    vital = next(o for o in fake_db.added if isinstance(o, MonitorWebVital))
    assert vital.metric == "LCP"
    assert vital.value == 2300.0
    # Rating is derived from thresholds when the client doesn't send one.
    assert vital.rating == "good"
    assert source.last_seen_at is not None


async def test_ingest_vital_rejects_bad_metric(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])
    resp = await client.post(
        "/public/portfolio/vitals",
        json={"product_id": source.pipeline_id, "key": source.public_key, "metric": "BOGUS", "value": 1},
    )
    assert resp.status_code == 422


async def test_get_experience_vitals(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[]),          # app settings overlay
        FakeResult(rows=[source]),    # usage source
        FakeResult(rows=[("LCP", 10, 2200.0), ("CLS", 8, 0.05)]),  # metric, count, p75
        FakeResult(rows=[("LCP", "good", 7), ("LCP", "poor", 3), ("CLS", "good", 8)]),  # rating dist
        FakeResult(rows=[("https://example.com/", "LCP", 6, 2100.0), ("https://example.com/", "CLS", 5, 0.04)]),
    ])

    resp = await client.get(f"/portfolio/{product.id}/experience", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["metrics"][0]["metric"] == "LCP"  # canonical order
    lcp = body["metrics"][0]
    assert lcp["p75"] == 2200.0
    assert lcp["rating"] == "good"
    assert lcp["good"] == 7 and lcp["poor"] == 3
    cls = next(m for m in body["metrics"] if m["metric"] == "CLS")
    assert cls["p75"] == 0.05  # CLS keeps precision
    page = body["pages"][0]
    assert page["url"] == "https://example.com/"
    assert page["sampleCount"] == 11
    assert page["metrics"]["LCP"]["p75"] == 2100.0


async def test_ingest_log(client, fake_db):
    from app.models import MonitorLog

    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/logs",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "level": "warn",
            "message": "slow query",
            "session_id": "s1",
        },
    )

    assert resp.status_code == 200
    log = next(o for o in fake_db.added if isinstance(o, MonitorLog))
    assert log.level == "warn"
    assert log.message == "slow query"
    assert source.last_seen_at is not None


async def test_ingest_log_rejects_bad_level(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])
    resp = await client.post(
        "/public/portfolio/logs",
        json={"product_id": source.pipeline_id, "key": source.public_key, "level": "trace", "message": "x"},
    )
    assert resp.status_code == 422


async def test_get_logs_faceted(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)

    class _Log:
        def __init__(self, level, message):
            self.id = "log-1"
            self.level = level
            self.message = message
            self.url = "https://example.com/"
            self.session_id = "s1"
            self.user_ref = None
            self.release = "v1"
            self.event_metadata = {}
            self.occurred_at = datetime(2026, 5, 20, tzinfo=timezone.utc)

    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[]),          # app settings overlay
        FakeResult(rows=[source]),    # usage source
        FakeResult(rows=[("info", 5), ("warn", 2), ("error", 1)]),  # level facet
        FakeResult(rows=[_Log("warn", "slow query")]),              # log lines
    ])

    resp = await client.get(f"/portfolio/{product.id}/logs?q=slow", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["levelCounts"] == {"debug": 0, "info": 5, "warn": 2, "error": 1}
    assert body["filters"]["q"] == "slow"
    assert body["logs"][0]["message"] == "slow query"
    assert body["logs"][0]["level"] == "warn"


async def test_get_command_center(client, fake_db, auth_headers):
    from datetime import datetime as _dt, timezone as _tz

    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id, last_seen_at=_dt.now(_tz.utc))
    revenue = make_revenue_source(
        pipeline_id=product.id,
        status="connected",
        provider_account_id="acct_123",
        current_mrr_cents=10000,
        previous_mrr_cents=8000,
    )
    group = make_error_group(pipeline_id=product.id, title="Boom", event_count=12)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),   # launched product lookup
            FakeResult(rows=[]),          # app settings overlay
            FakeResult(rows=[source]),    # usage source
            FakeResult(rows=[revenue]),   # revenue source
            FakeResult(rows=[group]),     # top open issues
        ],
        scalar=[
            100, 5, 100, None,            # health: total, errors, sessions, lcp_p75
            60, 40,                       # visitors current/prev
            12, 8,                        # signups current/prev
            3, 5,                         # errors current/prev
        ],
    )

    resp = await client.get(f"/portfolio/{product.id}/command-center", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["health"]["state"] == "live"   # fresh, 5% error rate
    assert body["trends"]["visitors"]["changePct"] == 0.5   # (60-40)/40
    assert body["trends"]["errors"]["current"] == 3
    assert body["trends"]["revenue"]["current"] == 10000
    assert body["trends"]["revenue"]["changePct"] == 0.25   # (10000-8000)/8000
    assert body["revenueConnected"] is True
    assert body["topIssues"][0]["title"] == "Boom"


def test_health_verdict_v2_states():
    from app.config import get_settings
    from app.services.monitoring.analytics import _health_verdict_v2

    eff = get_settings()  # warn=24h, unhealthy=72h, noisy=0.1, failing=0.3
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def at(hours):
        return now - timedelta(hours=hours)

    def state(**kw):
        base = dict(connected=True, total_events=10, error_rate=0.0, lcp_rating="good", eff=eff, now=now)
        base.update(kw)
        return _health_verdict_v2(**base)["state"]

    assert state(connected=False, last_seen_at=None) == "no-data"
    assert state(last_seen_at=None, total_events=0) == "no-data"
    assert state(last_seen_at=at(100)) == "silent"             # past unhealthy hours
    assert state(last_seen_at=at(1), error_rate=0.5) == "failing"   # rate >= 0.3
    assert state(last_seen_at=at(1), error_rate=0.0, lcp_rating="poor") == "failing"
    assert state(last_seen_at=at(1), error_rate=0.15) == "noisy"    # rate >= 0.1
    assert state(last_seen_at=at(1), error_rate=0.0, lcp_rating="needs-improvement") == "noisy"
    assert state(last_seen_at=at(36)) == "stale"               # warn < age < unhealthy, clean
    assert state(last_seen_at=at(1)) == "live"                 # fresh + clean


@pytest.mark.parametrize("prefix", ["/portfolio", "/monitor"])
async def test_get_source_health_failing(client, fake_db, auth_headers, prefix):
    from datetime import datetime as _dt, timezone as _tz

    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id, last_seen_at=_dt.now(_tz.utc))
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),   # launched product lookup
            FakeResult(rows=[]),          # app settings overlay
            FakeResult(rows=[source]),    # usage source
        ],
        scalar=[100, 50, 100, None],  # total_events, errors, sessions, lcp_p75
    )

    resp = await client.get(f"{prefix}/{product.id}/health", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["health"]["state"] == "failing"   # 50/100 = 0.5 error rate
    assert body["signals"]["errorRate"] == 0.5
    assert body["signals"]["lcpRating"] is None


def test_health_verdict_states():
    from app.config import get_settings
    from app.services.monitoring.analytics import _health_verdict

    eff = get_settings()  # health_warning_hours=24, health_unhealthy_hours=72
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    # No source / no events → no-data.
    assert _health_verdict(connected=False, last_seen_at=None, total_events=0, eff=eff, now=now)["state"] == "no-data"
    assert _health_verdict(connected=True, last_seen_at=None, total_events=0, eff=eff, now=now)["state"] == "no-data"

    # Fresh → healthy; quiet → warning; long silent → unhealthy.
    fresh = now - timedelta(hours=2)
    quiet = now - timedelta(hours=36)
    silent = now - timedelta(hours=100)
    assert _health_verdict(connected=True, last_seen_at=fresh, total_events=10, eff=eff, now=now)["state"] == "healthy"
    assert _health_verdict(connected=True, last_seen_at=quiet, total_events=10, eff=eff, now=now)["state"] == "warning"
    assert _health_verdict(connected=True, last_seen_at=silent, total_events=10, eff=eff, now=now)["state"] == "unhealthy"


@pytest.mark.parametrize("public_prefix", ["/public/portfolio", "/public/monitor"])
async def test_public_usage_event_persists_release_and_environment(client, fake_db, public_prefix):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        f"{public_prefix}/events",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "event_type": "pageview",
            "release": "v3.1.0",
            "environment": "production",
        },
    )

    assert resp.status_code == 200
    event = next(o for o in fake_db.added if isinstance(o, MonitorUsageEvent))
    assert event.release == "v3.1.0"
    assert event.environment == "production"


async def test_ingest_rate_limit_returns_429(client, fake_db, monkeypatch):
    from app.config import get_settings

    monitor_ingest._rate_windows.clear()
    monkeypatch.setattr(get_settings(), "ingest_rate_limit_per_minute", 1)

    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source]), FakeResult(rows=[source])])
    payload = {"product_id": source.pipeline_id, "key": source.public_key, "event_type": "pageview"}

    first = await client.post("/public/portfolio/events", json=payload)
    assert first.status_code == 200
    # Second event in the same minute window trips the per-source limit.
    second = await client.post("/public/portfolio/events", json=payload)
    assert second.status_code == 429
    assert second.headers.get("Retry-After") == "60"

    monitor_ingest._rate_windows.clear()


async def test_public_batch_ingests_mixed_events(client, fake_db):
    from app.models import MonitorErrorEvent, MonitorErrorGroup

    source = make_usage_source()
    fake_db.stub(execute=[
        FakeResult(rows=[source]),  # source lookup
        FakeResult(rows=[]),        # error item: no existing group → create
    ])

    resp = await client.post(
        "/public/portfolio/batch",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "batch": [
                {"kind": "event", "event_type": "pageview", "visitor_id": "v1", "session_id": "s1",
                 "url": "https://example.com/", "release": "v2", "environment": "production"},
                {"kind": "event", "event_type": "signup", "visitor_id": "v1", "session_id": "s1"},
                {"kind": "error", "message": "Boom", "stack": "at x (a.js:1:1)", "session_id": "s1"},
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["stored"] == 3
    usage = [o for o in fake_db.added if isinstance(o, MonitorUsageEvent)]
    assert {e.event_type for e in usage} == {"pageview", "signup"}
    # The error item routes through the shared grouping path.
    assert any(isinstance(o, MonitorErrorGroup) for o in fake_db.added)
    assert any(isinstance(o, MonitorErrorEvent) for o in fake_db.added)
    assert source.last_seen_at is not None


async def test_public_batch_rejects_bad_key(client, fake_db):
    fake_db.stub(execute=[FakeResult(rows=[])])

    resp = await client.post(
        "/public/portfolio/batch",
        json={"product_id": "p1", "key": "nope", "batch": [{"kind": "event", "event_type": "pageview"}]},
    )

    assert resp.status_code == 404


async def test_public_batch_skips_invalid_items(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    # An event with no event_type and an error with no message are both dropped.
    resp = await client.post(
        "/public/portfolio/batch",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "batch": [
                {"kind": "event"},
                {"kind": "error"},
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["stored"] == 0
    assert not any(isinstance(o, MonitorUsageEvent) for o in fake_db.added)


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
