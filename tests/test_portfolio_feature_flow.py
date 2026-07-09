"""Feature-flow graph: named user flows (from feature spans) with run count,
error count and mean duration, and feature->feature transitions per session."""

from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline
from app.services.monitoring.analytics import _feature_flow

T = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_feature_flow_aggregates_counts_errors_and_duration():
    # rows: (session_id, feature, status, duration_ms, start_at)
    rows = [
        ("s1", "signup", "ok", 100.0, T),
        ("s1", "checkout", "ok", 200.0, T),
        ("s1", "checkout", "error", 300.0, T),  # self-hop: no edge, but counted
        ("s2", "signup", "ok", 140.0, T),
        ("s2", "checkout", "ok", None, T),      # missing duration is skipped in the mean
    ]
    nodes, edges = _feature_flow(rows)

    by = {n["feature"]: n for n in nodes}
    assert by["checkout"]["count"] == 3
    assert by["checkout"]["errorCount"] == 1
    assert by["checkout"]["avgDurationMs"] == 250.0  # (200+300)/2, the None skipped
    assert by["signup"]["count"] == 2
    assert by["signup"]["errorCount"] == 0
    assert by["signup"]["avgDurationMs"] == 120.0
    # busiest feature first
    assert [n["feature"] for n in nodes] == ["checkout", "signup"]
    # signup->checkout happens once per session
    assert edges == [{"from": "signup", "to": "checkout", "count": 2}]


def test_feature_flow_does_not_cross_sessions():
    rows = [("s1", "checkout", "ok", 10.0, T), ("s2", "signup", "ok", 10.0, T)]
    nodes, edges = _feature_flow(rows)
    assert {n["feature"] for n in nodes} == {"checkout", "signup"}
    assert edges == []


async def test_get_feature_flow_endpoint(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[]),          # app settings overlay
        FakeResult(rows=[            # ordered feature spans
            ("s1", "signup", "ok", 120.0, T),
            ("s1", "checkout", "error", 400.0, T),
        ]),
    ])

    resp = await client.get(f"/portfolio/{product.id}/feature-flow", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    by = {n["feature"]: n for n in body["nodes"]}
    assert by["signup"]["count"] == 1 and by["signup"]["avgDurationMs"] == 120.0
    assert by["checkout"]["errorCount"] == 1
    assert body["edges"] == [{"from": "signup", "to": "checkout", "count": 1}]
