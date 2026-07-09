"""Journey / flow graph: page->page transitions aggregated from session-ordered
pageviews."""

from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline
from app.services.monitoring.analytics import _flow_graph

T = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_flow_graph_builds_nodes_and_edges():
    rows = [
        ("s1", "/a", T), ("s1", "/b", T), ("s1", "/c", T),
        ("s2", "/a", T), ("s2", "/b", T),
        ("s3", "/a", T), ("s3", "/a", T),  # self-hop (refresh) is dropped
    ]
    nodes, edges = _flow_graph(rows)

    assert nodes == [
        {"url": "/a", "visits": 4},
        {"url": "/b", "visits": 2},
        {"url": "/c", "visits": 1},
    ]
    assert edges == [
        {"from": "/a", "to": "/b", "count": 2},
        {"from": "/b", "to": "/c", "count": 1},
    ]


def test_flow_graph_does_not_cross_sessions():
    # The last page of s1 must not connect to the first page of s2.
    rows = [("s1", "/x", T), ("s2", "/y", T)]
    nodes, edges = _flow_graph(rows)
    assert {n["url"] for n in nodes} == {"/x", "/y"}
    assert edges == []


async def test_get_flow_endpoint(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[]),          # app settings overlay
        FakeResult(rows=[("s1", "/a", T), ("s1", "/b", T)]),  # ordered pageviews
    ])

    resp = await client.get(f"/portfolio/{product.id}/flow", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert {n["url"] for n in body["nodes"]} == {"/a", "/b"}
    assert body["edges"] == [{"from": "/a", "to": "/b", "count": 1}]
