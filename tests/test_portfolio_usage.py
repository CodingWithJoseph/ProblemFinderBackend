from datetime import datetime, timezone

from conftest import (
    FakeResult,
    make_pipeline,
    make_usage_event,
    make_usage_source,
)
from app.models import PortfolioUsageEvent, PortfolioUsageSource


async def test_portfolio_product_includes_usage_source(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),  # launched product lookup
        FakeResult(rows=[]),         # issue counts
        FakeResult(rows=[source]),   # usage source
    ])

    resp = await client.get(f"/portfolio/{product.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["id"] == product.id
    assert body["usageSource"]["publicKey"] == source.public_key


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
