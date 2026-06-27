"""The trace / incident-chain endpoint: assembling spans (as a parent->child
tree), errors, and logs that share a trace_id."""

from datetime import datetime, timezone

from conftest import FakeResult, make_error_event, make_pipeline
from app.models import MonitorSpan


def _span(pipeline_id, span_id, parent, *, name, kind="client", service="web", status="ok", duration=10.0, minute=0):
    return MonitorSpan(
        id=f"id-{span_id}",
        pipeline_id=pipeline_id,
        trace_id="T",
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        kind=kind,
        service=service,
        status=status,
        duration_ms=duration,
        start_at=datetime(2026, 6, 1, 12, minute, tzinfo=timezone.utc),
        received_at=datetime(2026, 6, 1, 12, minute, tzinfo=timezone.utc),
    )


async def test_get_trace_assembles_incident_chain(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    root = _span(product.id, "root", None, name="pageview /cart", minute=0)
    fetch = _span(product.id, "fetch1", "root", name="GET /api/cart", status="error", minute=1)
    server = _span(product.id, "srv1", "fetch1", name="GET /api/cart", kind="server", service="backend", status="error", duration=140.0, minute=2)
    err = make_error_event(pipeline_id=product.id, trace_id="T", span_id="fetch1", message="TypeError: boom")

    fake_db.stub(execute=[
        FakeResult(rows=[product]),            # _require_launched_product
        FakeResult(rows=[root, fetch, server]),  # spans
        FakeResult(rows=[err]),                # errors
        FakeResult(rows=[]),                   # logs
    ])

    resp = await client.get(f"/portfolio/{product.id}/traces/T", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["traceId"] == "T"

    # Preorder tree with depth: root -> fetch -> server.
    chain = [(s["spanId"], s["depth"]) for s in body["spans"]]
    assert chain == [("root", 0), ("fetch1", 1), ("srv1", 2)]

    assert body["summary"]["spanCount"] == 3
    assert body["summary"]["errorCount"] == 1
    assert body["summary"]["hasServer"] is True
    assert body["summary"]["services"] == ["web", "backend"]
    assert body["summary"]["durationMs"] == 140.0

    # The error is pinned to the trace + the span it fired in.
    assert body["errors"][0]["traceId"] == "T"
    assert body["errors"][0]["spanId"] == "fetch1"


async def test_get_trace_orphan_span_treated_as_root(client, fake_db, auth_headers):
    # A server span whose client parent was sampled out still shows (as a root),
    # never dropped.
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    orphan = _span(product.id, "srv1", "missing-parent", name="GET /api/x", kind="server", service="backend")

    fake_db.stub(execute=[
        FakeResult(rows=[product]),
        FakeResult(rows=[orphan]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
    ])

    resp = await client.get(f"/portfolio/{product.id}/traces/T", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert [(s["spanId"], s["depth"]) for s in body["spans"]] == [("srv1", 0)]
    assert body["summary"]["hasServer"] is True


async def test_get_trace_requires_launched_product(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # product lookup misses
    resp = await client.get("/portfolio/nope/traces/T", headers=auth_headers)
    assert resp.status_code == 404
