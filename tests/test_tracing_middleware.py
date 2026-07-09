"""Server tracing: traceparent parsing, server-span construction, and the ASGI
middleware that continues the beacon's trace into a server span."""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.services.tracing import (
    ServerTracingMiddleware,
    build_server_span,
    make_batch_reporter,
    parse_traceparent,
)

VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_parse_traceparent_valid():
    tp = parse_traceparent(VALID)
    assert tp is not None
    assert tp.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert tp.span_id == "b7ad6b7169203331"
    assert tp.flags == "01"


def test_parse_traceparent_is_case_insensitive_and_trims():
    tp = parse_traceparent("  00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01  ")
    assert tp is not None
    assert tp.trace_id == "0af7651916cd43dd8448eb211c80319c"


@pytest.mark.parametrize("header", [
    None,
    "",
    "garbage",
    "00-tooshort-b7ad6b7169203331-01",
    "ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",   # invalid version
    "00-" + "0" * 32 + "-b7ad6b7169203331-01",                    # all-zero trace id
    "00-0af7651916cd43dd8448eb211c80319c-" + "0" * 16 + "-01",    # all-zero span id
])
def test_parse_traceparent_rejects(header):
    assert parse_traceparent(header) is None


def test_build_server_span_continues_parent_trace():
    parent = parse_traceparent(VALID)
    span = build_server_span(method="get", path="/api/cart", status_code=200, duration_ms=12.3456, parent=parent, service="api")
    assert span["kind"] == "span"
    assert span["span_kind"] == "server"
    assert span["service"] == "api"
    assert span["trace_id"] == parent.trace_id
    assert span["parent_span_id"] == parent.span_id
    assert span["span_id"] != parent.span_id
    assert len(span["span_id"]) == 16
    assert span["name"] == "GET /api/cart"
    assert span["status"] == "ok"
    assert span["duration_ms"] == 12.346


def test_build_server_span_without_parent_starts_new_trace():
    span = build_server_span(method="POST", path="/x", status_code=503, duration_ms=1.0, parent=None)
    assert len(span["trace_id"]) == 32
    assert span["parent_span_id"] is None
    assert span["status"] == "error"  # 5xx is the server's fault


def _app(spans, *, route_status=200, reporter=None):
    async def ok(request):
        return JSONResponse({"ok": True}, status_code=route_status)

    async def capture(span):
        spans.append(span)

    app = Starlette(routes=[Route("/api/cart", ok), Route("/api/cart", ok, methods=["POST"])])
    app.add_middleware(ServerTracingMiddleware, report=reporter or capture, service="api")
    return app


def test_middleware_emits_server_span_continuing_trace():
    spans = []
    client = TestClient(_app(spans))
    resp = client.get("/api/cart", headers={"traceparent": VALID})
    assert resp.status_code == 200
    assert len(spans) == 1
    span = spans[0]
    assert span["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert span["parent_span_id"] == "b7ad6b7169203331"
    assert span["span_kind"] == "server"
    assert span["name"] == "GET /api/cart"
    assert span["status"] == "ok"


def test_middleware_starts_trace_without_header():
    spans = []
    client = TestClient(_app(spans))
    resp = client.get("/api/cart")
    assert resp.status_code == 200
    assert len(spans) == 1
    assert spans[0]["parent_span_id"] is None
    assert len(spans[0]["trace_id"]) == 32


def test_middleware_marks_5xx_as_error():
    spans = []
    client = TestClient(_app(spans, route_status=500))
    client.get("/api/cart", headers={"traceparent": VALID})
    assert spans[0]["status"] == "error"


def test_reporter_failure_never_breaks_the_request():
    async def boom(_span):
        raise RuntimeError("collector down")

    spans = []
    client = TestClient(_app(spans, reporter=boom))
    resp = client.get("/api/cart", headers={"traceparent": VALID})
    assert resp.status_code == 200  # request unaffected


@pytest.mark.asyncio
async def test_batch_reporter_posts_to_canonical_monitor_ingest(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            calls.append({"url": url, "json": json, "timeout": self.timeout})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    reporter = make_batch_reporter("https://collector.test/", "pipe-1", "key-1", timeout=1.5)
    await reporter({"kind": "span", "trace_id": "trace-1"})

    assert calls == [{
        "url": "https://collector.test/api/public/monitor/batch",
        "json": {
            "product_id": "pipe-1",
            "key": "key-1",
            "batch": [{"kind": "span", "trace_id": "trace-1"}],
        },
        "timeout": 1.5,
    }]
