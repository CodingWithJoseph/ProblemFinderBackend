"""Ingest persistence for the trace spine + error-type / platform / capture-mode
dimensions added in 0038. Verifies the optional fields, when sent, land on the
ORM rows the storage path builds (across the single endpoints and /batch)."""

from conftest import FakeResult, make_usage_source
from app.models import (
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorLog,
    MonitorSpan,
    MonitorUsageEvent,
    MonitorWebVital,
)


async def test_usage_event_persists_trace_and_dimensions(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/events",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "event_type": "pageview",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "parent_span_id": "span-0",
            "platform": "web",
            "capture_mode": "auto",
        },
    )

    assert resp.status_code == 200
    event = next(o for o in fake_db.added if isinstance(o, MonitorUsageEvent))
    assert event.trace_id == "trace-1"
    assert event.span_id == "span-1"
    assert event.parent_span_id == "span-0"
    assert event.platform == "web"
    assert event.capture_mode == "auto"


async def test_usage_event_defaults_trace_fields_to_none(client, fake_db):
    # Older snippets that don't send the new fields keep working.
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/events",
        json={"product_id": source.pipeline_id, "key": source.public_key, "event_type": "pageview"},
    )

    assert resp.status_code == 200
    event = next(o for o in fake_db.added if isinstance(o, MonitorUsageEvent))
    assert event.trace_id is None
    assert event.platform is None
    assert event.capture_mode is None


async def test_error_event_persists_type_and_trace_on_event_and_group(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source]), FakeResult(rows=[])])  # source, no existing group

    resp = await client.post(
        "/public/portfolio/errors",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "message": "TypeError: cannot read properties of undefined",
            "error_type": "exception",
            "trace_id": "trace-9",
            "span_id": "span-9",
            "platform": "web",
            "capture_mode": "manual",
        },
    )

    assert resp.status_code == 200
    event = next(o for o in fake_db.added if isinstance(o, MonitorErrorEvent))
    group = next(o for o in fake_db.added if isinstance(o, MonitorErrorGroup))
    assert event.error_type == "exception"
    assert event.trace_id == "trace-9"
    assert event.span_id == "span-9"
    assert event.platform == "web"
    assert event.capture_mode == "manual"
    # The group carries the dimension so the issues list can facet by it.
    assert group.error_type == "exception"


async def test_batch_persists_trace_fields_across_kinds(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/batch",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "batch": [
                {"kind": "vital", "metric": "LCP", "value": 2200.0, "trace_id": "t-vital", "platform": "web"},
                {"kind": "log", "log_level": "error", "message": "checkout failed", "trace_id": "t-log", "platform": "web"},
            ],
        },
    )

    assert resp.status_code == 200
    vital = next(o for o in fake_db.added if isinstance(o, MonitorWebVital))
    log = next(o for o in fake_db.added if isinstance(o, MonitorLog))
    assert vital.trace_id == "t-vital" and vital.platform == "web"
    assert log.trace_id == "t-log" and log.platform == "web"


async def test_batch_persists_span(client, fake_db):
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/batch",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "batch": [{
                "kind": "span",
                "trace_id": "t-1",
                "span_id": "s-1",
                "parent_span_id": "s-0",
                "name": "GET /api/cart",
                "span_kind": "client",
                "service": "web",
                "feature": "checkout",
                "status": "error",
                "duration_ms": 821.5,
                "platform": "web",
                "capture_mode": "auto",
            }],
        },
    )

    assert resp.status_code == 200
    span = next(o for o in fake_db.added if isinstance(o, MonitorSpan))
    assert span.trace_id == "t-1" and span.span_id == "s-1" and span.parent_span_id == "s-0"
    assert span.name == "GET /api/cart"
    assert span.kind == "client"
    assert span.service == "web" and span.feature == "checkout"
    assert span.status == "error"
    assert span.duration_ms == 821.5
    assert span.platform == "web" and span.capture_mode == "auto"


async def test_batch_span_requires_identity(client, fake_db):
    # Missing trace_id / span_id / name → skipped, not stored.
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/batch",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "batch": [{"kind": "span", "name": "no ids"}],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["stored"] == 0
    assert not any(isinstance(o, MonitorSpan) for o in fake_db.added)


async def test_batch_span_clamps_unknown_kind(client, fake_db):
    # A malformed span kind falls back to 'client' so it can't trip the CHECK.
    source = make_usage_source()
    fake_db.stub(execute=[FakeResult(rows=[source])])

    resp = await client.post(
        "/public/portfolio/batch",
        json={
            "product_id": source.pipeline_id,
            "key": source.public_key,
            "batch": [{"kind": "span", "trace_id": "t", "span_id": "s", "name": "x", "span_kind": "bogus"}],
        },
    )

    assert resp.status_code == 200
    span = next(o for o in fake_db.added if isinstance(o, MonitorSpan))
    assert span.kind == "client"
