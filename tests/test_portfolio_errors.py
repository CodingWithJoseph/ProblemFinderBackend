from datetime import datetime, timezone

from conftest import (
    FakeResult,
    make_error_event,
    make_error_group,
    make_pipeline,
    make_usage_source,
)
from app.models import MonitorErrorEvent, MonitorErrorGroup
from app.services.monitoring.ingest import _error_fingerprint, _normalize_error_message


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

    group = next(o for o in fake_db.added if isinstance(o, MonitorErrorGroup))
    event = next(o for o in fake_db.added if isinstance(o, MonitorErrorEvent))
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
    assert not any(isinstance(o, MonitorErrorGroup) for o in fake_db.added)
    assert any(isinstance(o, MonitorErrorEvent) for o in fake_db.added)


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


async def test_get_problems_list(client, fake_db, auth_headers):
    from app.models import MonitorProblem

    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    problem = MonitorProblem(
        id="prob-1", pipeline_id=product.id, kind="error_spike", dedupe_key="2026-05-20",
        title="Error spike", detail="60 vs 10", severity="critical", status="open",
        metric="errors", baseline=10.0, observed=60.0,
        detected_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    fake_db.stub(execute=[
        FakeResult(rows=[product]),
        FakeResult(rows=[problem]),
    ])

    resp = await client.get(f"/portfolio/{product.id}/problems", headers=auth_headers)

    assert resp.status_code == 200
    problems = resp.json()["data"]["problems"]
    assert problems[0]["kind"] == "error_spike"
    assert problems[0]["observed"] == 60.0
    assert problems[0]["severity"] == "critical"


async def test_get_problem_impact(client, fake_db, auth_headers):
    from app.models import MonitorProblem

    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    problem = MonitorProblem(
        id="prob-1", pipeline_id=product.id, kind="error_spike", dedupe_key="2026-05-20",
        title="Error spike", severity="critical", status="open",
        metric="errors", baseline=10.0, observed=60.0,
        detected_at=datetime(2026, 5, 20, 12, tzinfo=timezone.utc),
    )
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),
            FakeResult(rows=[problem]),
        ],
        scalar=[8, 60, 15],  # before, during, after
    )

    resp = await client.get(f"/portfolio/{product.id}/problems/prob-1?windowHours=24", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["problem"]["id"] == "prob-1"
    assert body["impact"] == {"metric": "errors", "windowHours": 24, "before": 8, "during": 60, "after": 15}


async def test_get_problem_impact_404(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[])])
    resp = await client.get(f"/portfolio/{product.id}/problems/nope", headers=auth_headers)
    assert resp.status_code == 404


async def test_get_issue_sessions(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    group = make_error_group(pipeline_id=product.id)
    t1 = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 20, 10, 4, tzinfo=timezone.utc)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[]),          # app settings overlay
        FakeResult(rows=[group]),     # group lookup
        FakeResult(rows=[            # session, visitor, user_ref, occurrences, first, last
            ("sess-1", "v1", "user-9", 3, t1, t2),
            ("sess-2", "v2", None, 1, t1, t1),
        ]),
    ])

    resp = await client.get(f"/portfolio/{product.id}/issues/{group.id}/sessions", headers=auth_headers)

    assert resp.status_code == 200
    sessions = resp.json()["data"]["sessions"]
    assert sessions[0]["sessionId"] == "sess-1"
    assert sessions[0]["userRef"] == "user-9"
    assert sessions[0]["identified"] is True
    assert sessions[0]["occurrences"] == 3
    assert sessions[1]["identified"] is False


async def test_get_issue_sessions_404(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),   # group lookup → none
    ])
    resp = await client.get(f"/portfolio/{product.id}/issues/nope/sessions", headers=auth_headers)
    assert resp.status_code == 404


async def test_patch_issue_status(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    group = make_error_group(pipeline_id=product.id, status="unresolved")
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[group]),     # group lookup
    ])

    resp = await client.patch(
        f"/portfolio/{product.id}/issues/{group.id}",
        json={"status": "resolved"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "resolved"
    assert group.status == "resolved"


async def test_patch_issue_status_404(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[])])
    resp = await client.patch(
        f"/portfolio/{product.id}/issues/nope",
        json={"status": "resolved"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_patch_issue_status_rejects_bad_status(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product])])
    resp = await client.patch(
        f"/portfolio/{product.id}/issues/x",
        json={"status": "bogus"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_get_errors_by_release(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    t_old = datetime(2026, 5, 18, tzinfo=timezone.utc)
    t_new = datetime(2026, 5, 22, tzinfo=timezone.utc)
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[]),          # app settings overlay
        FakeResult(rows=[source]),    # usage source
        FakeResult(rows=[            # error agg per release: release, errors, users, sessions, first, last
            ("v1.9.0", 20, 8, 10, t_old, t_old),
            ("v2.0.0", 60, 25, 30, t_new, t_new),
        ]),
        FakeResult(rows=[("v1.9.0", 200), ("v2.0.0", 250)]),  # usage sessions per release
    ])

    resp = await client.get(f"/portfolio/{product.id}/errors/by-release", headers=auth_headers)

    assert resp.status_code == 200
    releases = resp.json()["data"]["releases"]
    # Most recent release leads.
    assert releases[0]["release"] == "v2.0.0"
    assert releases[0]["errors"] == 60
    assert releases[0]["sessions"] == 250
    assert releases[0]["errorsPerSession"] == 0.24  # 60 / 250
    older = next(r for r in releases if r["release"] == "v1.9.0")
    assert older["errorsPerSession"] == 0.1  # 20 / 200 — the deploy regressed


async def test_get_issue_detail_facets(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    group = make_error_group(pipeline_id=product.id, event_count=40, title="Boom")
    sample = make_error_event(pipeline_id=product.id, group_id=group.id, message="Boom", stack="at x (a.js:1:1)")
    fake_db.stub(execute=[
        FakeResult(rows=[product]),                 # launched product lookup
        FakeResult(rows=[]),                        # app settings overlay
        FakeResult(rows=[group]),                   # group lookup
        FakeResult(rows=[(9, 12, 40, 30, 10)]),     # agg: users, sessions, occ, recent, prior
        FakeResult(rows=[("v2.0.0", 25, 6), ("v1.9.0", 15, 4)]),  # release facet
        FakeResult(rows=[("https://example.com/app", 30, 8)]),    # url facet
        FakeResult(rows=[sample]),                  # sample event
        FakeResult(rows=[sample]),                  # recent occurrences
    ])

    resp = await client.get(f"/portfolio/{product.id}/issues/{group.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["issue"]["title"] == "Boom"
    assert body["issue"]["affectedUsers"] == 9
    assert body["issue"]["trend"]["direction"] == "up"
    assert body["sample"]["message"] == "Boom"
    assert body["facets"]["release"][0] == {"value": "v2.0.0", "count": 25, "users": 6}
    assert body["facets"]["url"][0]["value"] == "https://example.com/app"
    assert len(body["recentOccurrences"]) == 1


async def test_get_issue_detail_404(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),   # group lookup → none
    ])
    resp = await client.get(f"/portfolio/{product.id}/issues/nope", headers=auth_headers)
    assert resp.status_code == 404


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


async def test_get_error_metrics_faceting(client, fake_db, auth_headers):
    # The errorType/platform filters scope the issue list + recent feed, and the
    # response carries the available facet values (with counts) for the chips.
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    group = make_error_group(pipeline_id=product.id, event_count=5, error_type="exception")
    event = make_error_event(pipeline_id=product.id, group_id=group.id, error_type="exception", platform="web")
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),            # launched product lookup
            FakeResult(rows=[]),                   # app settings overlay
            FakeResult(rows=[source]),             # usage source
            FakeResult(rows=[("2026-05-01", 5)]),  # daily error counts
            FakeResult(rows=[group]),              # top issues (filtered)
            FakeResult(rows=[(group.id, 3)]),      # sessions per group
            FakeResult(rows=[event]),              # recent errors (filtered)
            FakeResult(rows=[("exception", 5), ("unhandled_rejection", 2)]),  # errorType facets
            FakeResult(rows=[("web", 4)]),         # platform facets
        ],
        scalar=[5, 5, 3, 10, 1],  # total, errors14d, affectedSessions, usageSessions, openIssues
    )

    resp = await client.get(
        f"/portfolio/{product.id}/errors?errorType=exception&platform=web",
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["filters"] == {"errorType": "exception", "platform": "web"}
    assert body["facets"]["errorType"] == [
        {"value": "exception", "count": 5},
        {"value": "unhandled_rejection", "count": 2},
    ]
    assert body["facets"]["platform"] == [{"value": "web", "count": 4}]
    assert body["issues"][0]["errorType"] == "exception"
    assert body["recentErrors"][0]["errorType"] == "exception"
    assert body["recentErrors"][0]["platform"] == "web"


async def test_get_issues_faceting(client, fake_db, auth_headers):
    # errorType/platform filters scope the promoted issue list, and the response
    # carries the available facet values for the chips + the active selection.
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    source = make_usage_source(pipeline_id=product.id)
    group = make_error_group(pipeline_id=product.id, event_count=40, error_type="exception")
    fake_db.stub(
        execute=[
            FakeResult(rows=[product]),                 # launched product lookup
            FakeResult(rows=[]),                        # app settings overlay
            FakeResult(rows=[source]),                  # usage source
            FakeResult(rows=[(group.id, 9, 12, 40, 30, 10)]),  # per-group aggregation
            FakeResult(rows=[group]),                   # groups (filtered)
            FakeResult(rows=[("exception", 3)]),        # errorType facets
            FakeResult(rows=[("web", 2), ("mobile", 1)]),  # platform facets
        ],
        scalar=[1, 9, 40],  # openIssues, affectedUsers, occurrences
    )

    resp = await client.get(
        f"/portfolio/{product.id}/issues?errorType=exception&platform=web",
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["filters"] == {"errorType": "exception", "platform": "web"}
    assert body["facets"]["errorType"] == [{"value": "exception", "count": 3}]
    assert body["facets"]["platform"] == [{"value": "web", "count": 2}, {"value": "mobile", "count": 1}]
    assert body["issues"][0]["errorType"] == "exception"
