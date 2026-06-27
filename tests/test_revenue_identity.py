"""Tests for the identity join (PR3): resolution logic + identify endpoint."""

from datetime import datetime, timezone

from conftest import FakeResult, make_usage_source
from app.models import MonitorIdentity
from app.routes.portfolio import _resolve_identity, _join_coverage


# ── resolution logic ──────────────────────────────────────────────────────────

def test_explicit_id_wins():
    cid, method = _resolve_identity("cus_explicit", "a@b.com", {"a@b.com": "cus_email"})
    assert (cid, method) == ("cus_explicit", "explicit")


def test_email_fallback_matches_directory():
    cid, method = _resolve_identity(None, "User@Example.com", {"user@example.com": "cus_42"})
    assert (cid, method) == ("cus_42", "email")


def test_unresolved_when_no_match():
    assert _resolve_identity(None, "nobody@x.com", {}) == (None, "unresolved")
    assert _resolve_identity(None, None, {"a@b.com": "cus_1"}) == (None, "unresolved")


# ── identify endpoint ─────────────────────────────────────────────────────────

async def test_identify_explicit_join(client, fake_db):
    usage = make_usage_source(pipeline_id="11111111-1111-1111-1111-111111111111", allowed_domain=None)
    fake_db.stub(execute=[
        FakeResult(rows=[usage]),  # usage source lookup
        FakeResult(rows=[]),       # revenue source lookup (none) -> empty email index
        FakeResult(rows=[]),       # existing identity lookup -> none, insert
    ])

    resp = await client.post("/public/portfolio/identify", json={
        "product_id": usage.pipeline_id,
        "key": usage.public_key,
        "user_id": "user-1",
        "stripe_customer_id": "cus_direct",
    })

    assert resp.status_code == 200
    assert resp.json()["resolution"] == "explicit"
    created = [o for o in fake_db.added if isinstance(o, MonitorIdentity)]
    assert created and created[0].stripe_customer_id == "cus_direct"
    assert created[0].resolution_method == "explicit"


async def test_identify_unknown_source_404(client, fake_db):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.post("/public/portfolio/identify", json={
        "product_id": "missing", "key": "nope", "user_id": "user-1",
    })
    assert resp.status_code == 404


# ── join-coverage report ──────────────────────────────────────────────────────

async def test_join_coverage_counts(fake_db):
    fake_db.stub(execute=[
        FakeResult(rows=[("explicit", 3), ("email", 2), ("unresolved", 5)]),
    ])
    coverage = await _join_coverage(fake_db, "pipe-1")
    assert coverage == {
        "total": 10, "explicit": 3, "email": 2, "unresolved": 5,
        "resolvedRate": round(5 / 10, 4),
    }
