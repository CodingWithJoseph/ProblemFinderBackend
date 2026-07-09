"""Server-side tracing for the incident chain.

A drop-in ASGI middleware a product's backend mounts to *continue* the trace the
beacon started in the browser: it reads the W3C ``traceparent`` header, times the
request, and reports a ``server`` span back to the portfolio ``/batch`` ingest.
With the client fetch span (from the beacon) and this server span sharing a
trace, a browser error can be tied to the backend request that caused it.

The core (``parse_traceparent`` / ``build_server_span``) is framework-agnostic
and pure; ``ServerTracingMiddleware`` + ``make_batch_reporter`` are the
batteries-included Starlette/httpx path. Tracing must never break the host
request, so reporting failures are swallowed.
"""

from __future__ import annotations

import inspect
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# version-traceid-spanid-flags, all lower-hex (W3C trace-context).
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

SpanReporter = Callable[[dict], "Awaitable[None] | None"]


@dataclass(frozen=True)
class Traceparent:
    version: str
    trace_id: str
    span_id: str
    flags: str


def parse_traceparent(header: str | None) -> Traceparent | None:
    """Parse a W3C ``traceparent``. Returns ``None`` when absent, malformed, a
    future/invalid version (``ff``), or carrying the all-zero (invalid) trace or
    span id. Lenient on surrounding whitespace/case, strict on shape."""
    if not header:
        return None
    match = _TRACEPARENT_RE.match(header.strip().lower())
    if not match:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return Traceparent(version, trace_id, span_id, flags)


def new_trace_id() -> str:
    return uuid.uuid4().hex  # 32 hex chars


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]  # 16 hex chars


def build_server_span(
    *,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    parent: Traceparent | None,
    service: str = "backend",
    release: str | None = None,
    environment: str | None = None,
    started_at: datetime | None = None,
) -> dict:
    """Build a ``server`` span item shaped for the portfolio ``/batch`` ingest
    (``kind="span"``). When a parent traceparent is present the span continues
    that trace and points at the incoming span; otherwise it starts a new trace."""
    return {
        "kind": "span",
        "trace_id": parent.trace_id if parent else new_trace_id(),
        "span_id": new_span_id(),
        "parent_span_id": parent.span_id if parent else None,
        "name": f"{method.upper()} {path}",
        "span_kind": "server",
        "service": service,
        # 5xx is the server's own fault; 4xx is the caller's, so the span is "ok".
        "status": "error" if status_code >= 500 else "ok",
        "duration_ms": round(float(duration_ms), 3),
        "release": release,
        "environment": environment,
        "occurred_at": (started_at or datetime.now(timezone.utc)).isoformat(),
        "metadata": {
            "http.method": method.upper(),
            "http.target": path,
            "http.status_code": status_code,
        },
    }


class ServerTracingMiddleware(BaseHTTPMiddleware):
    """Continue the beacon's trace and report a server span per request.

    ``report`` receives the span dict; it may be sync or async. ``sample`` is the
    fraction of requests to trace (1.0 = all). Any exception in span building or
    reporting is swallowed — instrumentation must never affect the response."""

    def __init__(self, app, *, report: SpanReporter, service: str = "backend", sample: float = 1.0, release: str | None = None, environment: str | None = None):
        super().__init__(app)
        self._report = report
        self._service = service
        self._sample = sample
        self._release = release
        self._environment = environment

    async def dispatch(self, request: Request, call_next):
        traced = self._sample >= 1.0 or random.random() < self._sample
        parent = parse_traceparent(request.headers.get("traceparent")) if traced else None
        start = time.perf_counter()
        status_code = 500  # default covers an exception in the handler
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            if traced:
                await self._emit(request, parent, status_code, (time.perf_counter() - start) * 1000.0)

    async def _emit(self, request: Request, parent: Traceparent | None, status_code: int, duration_ms: float) -> None:
        try:
            span = build_server_span(
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=duration_ms,
                parent=parent,
                service=self._service,
                release=self._release,
                environment=self._environment,
            )
            result = self._report(span)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass  # never let tracing break the request


def make_batch_reporter(endpoint: str, product_id: str, key: str, *, timeout: float = 2.0) -> SpanReporter:
    """A reporter that POSTs each span to the monitor ``/batch`` ingest. Uses a
    short timeout and swallows transport errors so a slow/unreachable collector
    can't stall the host request."""
    import httpx

    url = endpoint.rstrip("/") + "/api/public/monitor/batch"

    async def report(span: dict) -> None:
        payload = {"product_id": product_id, "key": key, "batch": [span]}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                await client.post(url, json=payload)
        except Exception:
            pass

    return report


def instrument(app, *, product_id: str, key: str, endpoint: str, service: str = "backend", sample: float = 1.0, release: str | None = None, environment: str | None = None):
    """Convenience: wire ``ServerTracingMiddleware`` with the batch reporter onto
    a Starlette/FastAPI app in one call."""
    app.add_middleware(
        ServerTracingMiddleware,
        report=make_batch_reporter(endpoint, product_id, key),
        service=service,
        sample=sample,
        release=release,
        environment=environment,
    )
    return app
