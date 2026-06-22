from datetime import datetime, timezone

from conftest import (
    FakeResult,
    make_error_event,
    make_error_group,
    make_pipeline,
    make_usage_source,
)
from app.models import PortfolioErrorEvent, PortfolioErrorGroup
from app.routes.portfolio import _error_fingerprint, _normalize_error_message


def test_fingerprint_groups_numeric_variants():
    # Same shape, different ids/line numbers → one issue.
    a = _error_fingerprint("User 123 not found", "at load (app.js:10:5)")
    b = _error_fingerprint("User 4567 not found", "at load (app.js:88:9)")
    assert a == b


def test_fingerprint_distinguishes_different_errors():
    a = _error_fingerprint("Null pointer", "at load (app.js:1:1)")
    b = _error_fingerprint("Network timeout", "at load (app.js:1:1)")
    assert a != b


def test_normalize_strips_volatile_tokens():
    assert _normalize_error_message("Order 99 failed at 0xAB12") == "Order <n> failed at <hex>"


async def test_ingest_error_creates_group_and_event(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[
        FakeResult(rows=[source]),  # source lookup
        FakeResult(rows=[]),        # no existing group → create
    ])

    resp = await client.post(
        "/public/portfolio/errors",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "message": "TypeError: cannot read properties of undefined",
            "stack": "at render (app.js:42:7)",
            "session_id": "session-1",
            "release": "v1.2.0",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["fingerprint"]

    group = next(o for o in fake_db.added if isinstance(o, PortfolioErrorGroup))
    event = next(o for o in fake_db.added if isinstance(o, PortfolioErrorEvent))
    assert group.event_count == 1
    assert group.pipeline_id == source.pipeline_id
    assert event.group_id == group.id
    assert event.fingerprint == group.fingerprint
    assert event.session_id == "session-1"
    assert source.last_seen_at is not None


async def test_ingest_error_increments_existing_group_and_reopens(client, fake_db):
    source = make_usage_source()
    group = make_error_group(pipeline_id=source.pipeline_id, event_count=4, status="resolved")
    fake_db.stub(execute=[
        FakeResult(rows=[source]),  # source lookup
        FakeResult(rows=[group]),   # existing group
    ])

    resp = await client.post(
        "/public/portfolio/errors",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "message": "Boom",
        },
    )

    assert resp.status_code == 200
    assert group.event_count == 5
    # Regression: a resolved issue recurring reopens it.
    assert group.status == "unresolved"
    # No second group should be created.
    assert not any(isinstance(o, PortfolioErrorGroup) for o in fake_db.added)
    assert any(isinstance(o, PortfolioErrorEvent) for o in fake_db.added)


async def test_ingest_error_rejects_bad_key(client, fake_db):
    fake_db.stub(execute=[FakeResult(rows=[])])

    resp = await client.post(
        "/public/portfolio/errors",
        json={"product_id": "p1", "key": "nope", "message": "Boom"},
    )

    assert resp.status_code == 404


async def test_ingest_error_rejects_unapproved_domain(client, fake_db):
    source = make_usage_source(allowed_domain="example.com")
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/errors",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "message": "Boom",
            "url": "https://not-example.test/app",
        },
    )

    assert resp.status_code == 403


async def test_get_error_metrics(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    group = make_error_group(pipeline_id=product.id, event_count=5)
    event = make_error_event(pipeline_id=product.id, group_id=group.id)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),          # launched product lookup
            FakeResult(rows=[]),                 # app settings overlay
            FakeResult(rows=[source]),           # usage source
            FakeResult(rows=[("2026-05-01", 5)]),  # daily error counts
            FakeResult(rows=[group]),            # top issues
            FakeResult(rows=[(group.id, 3)]),    # sessions per group
            FakeResult(rows=[event]),            # recent errors
        ],
        scalar=[5, 5, 3, 10, 1],  # total, errors14d, affectedSessions, usageSessions, openIssues
    )

    resp = await client.get(f"/portfolio/{product.id}/errors", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["connected"] is True
    assert body["totalErrors"] == 5
    assert body["summary14d"]["errors"] == 5
    assert body["summary14d"]["openIssues"] == 1
    assert body["summary14d"]["affectedSessions"] == 3
    assert body["summary14d"]["errorsPerSession"] == 0.5  # 5 errors / 10 sessions
    assert body["issues"][0]["eventCount"] == 5
    assert body["issues"][0]["affectedSessions"] == 3
    assert body["recentErrors"][0]["message"].startswith("TypeError")


async def test_get_issues_promotes_groups(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    big = make_error_group(pipeline_id=product.id, event_count=40, fingerprint="fp-big", title="Big")
    small = make_error_group(pipeline_id=product.id, event_count=5, fingerprint="fp-small", title="Small")
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),   # launched product lookup
            FakeResult(rows=[]),          # app settings overlay
            FakeResult(rows=[source]),    # usage source
            FakeResult(rows=[            # per-group aggregation: group_id, users, sessions, occ, recent, prior
                (small.id, 2, 3, 5, 1, 4),
                (big.id, 9, 12, 40, 30, 10),
            ]),
            FakeResult(rows=[small, big]),  # groups (unordered)
        ],
        scalar=[2, 11, 45],  # openIssues, affectedUsers, occurrences
    )

    resp = await client.get(f"/portfolio/{product.id}/issues", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["summary"] == {"openIssues": 2, "affectedUsers": 11, "occurrences": 45}
    # Ranked by affected users: the bigger-impact issue leads.
    assert body["issues"][0]["title"] == "Big"
    assert body["issues"][0]["affectedUsers"] == 9
    assert body["issues"][0]["affectedSessions"] == 12
    assert body["issues"][0]["occurrences"] == 40
    assert body["issues"][0]["trend"]["direction"] == "up"   # 30 recent vs 10 prior
    assert body["issues"][1]["title"] == "Small"
    assert body["issues"][1]["trend"]["direction"] == "down"  # 1 recent vs 4 prior


async def test_get_issues_handles_group_without_events(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    group = make_error_group(pipeline_id=product.id, event_count=3)
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),
            FakeResult(rows=[]),
            FakeResult(rows=[source]),
            FakeResult(rows=[]),          # no per-group aggregation in window
            FakeResult(rows=[group]),     # group still present
        ],
        scalar=[1, 0, 0],
    )

    resp = await client.get(f"/portfolio/{product.id}/issues", headers=auth_headers)
    assert resp.status_code == 200
    issue = resp.json()["data"]["issues"][0]
    assert issue["affectedUsers"] == 0
    assert issue["occurrences"] == 0
    assert issue["trend"]["direction"] == "flat"
