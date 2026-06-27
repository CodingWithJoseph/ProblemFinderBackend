"""Tests for first-party (own-key) revenue mode (PR1.5).

Cover the credential-routing helpers (the only branch in the read path) and the
admin-only first-party creation endpoint. The engine math is unchanged and is
covered by test_revenue_engine.
"""

from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline, make_revenue_source, make_subscription
from app.models import PlanEnum, MonitorRevenueSource
from app.routes.portfolio import (
    _credential_path_label,
    _source_is_syncable,
    _stripe_account_kwargs,
)


# ── credential routing ────────────────────────────────────────────────────────

def test_first_party_passes_no_stripe_account():
    source = make_revenue_source(account_mode="first_party", provider_account_id=None)
    assert _stripe_account_kwargs(source) == {}
    assert "first_party" in _credential_path_label(source)


def test_connected_scopes_to_provider_account():
    source = make_revenue_source(account_mode="connected", provider_account_id="acct_123")
    assert _stripe_account_kwargs(source) == {"stripe_account": "acct_123"}
    assert "acct_123" in _credential_path_label(source)


def test_syncable_rules():
    # connected needs a provider account id
    assert _source_is_syncable(make_revenue_source(status="connected", provider_account_id="acct_1"))
    assert not _source_is_syncable(make_revenue_source(status="connected", provider_account_id=None))
    # first-party is syncable with no provider account id
    assert _source_is_syncable(make_revenue_source(status="connected", account_mode="first_party", provider_account_id=None))
    # must be connected status
    assert not _source_is_syncable(make_revenue_source(status="not_connected", account_mode="first_party"))
    assert not _source_is_syncable(None)


# ── admin creation endpoint ───────────────────────────────────────────────────

async def test_create_first_party_source_requires_admin(client, fake_db, auth_headers):
    fake_db.stub(scalar=[make_subscription(plan=PlanEnum.pro)])  # admin gate fails
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    resp = await client.post(f"/portfolio/{product.id}/revenue-source/first-party", headers=auth_headers)
    assert resp.status_code == 403


async def test_create_first_party_source_creates_source(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],  # admin gate
        execute=[
            FakeResult(rows=[product]),  # launched product lookup
            FakeResult(rows=[]),         # no existing revenue source
        ],
    )

    resp = await client.post(f"/portfolio/{product.id}/revenue-source/first-party", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["accountMode"] == "first_party"
    assert body["status"] == "connected"
    assert body["providerAccountId"] is None
    created = [o for o in fake_db.added if isinstance(o, MonitorRevenueSource)]
    assert created and created[0].account_mode == "first_party"


async def test_create_first_party_rejects_existing_connected_source(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    existing = make_revenue_source(pipeline_id=product.id, account_mode="connected", status="connected", provider_account_id="acct_x")
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],
        execute=[
            FakeResult(rows=[product]),    # launched product lookup
            FakeResult(rows=[existing]),   # existing connected source
        ],
    )

    resp = await client.post(f"/portfolio/{product.id}/revenue-source/first-party", headers=auth_headers)

    assert resp.status_code == 400


async def test_create_first_party_idempotent_for_existing_first_party(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    existing = make_revenue_source(pipeline_id=product.id, account_mode="first_party", status="connected", provider_account_id=None)
    fake_db.stub(
        scalar=[make_subscription(plan=PlanEnum.admin)],
        execute=[
            FakeResult(rows=[product]),
            FakeResult(rows=[existing]),
        ],
    )

    resp = await client.post(f"/portfolio/{product.id}/revenue-source/first-party", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["accountMode"] == "first_party"
