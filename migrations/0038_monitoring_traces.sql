-- 0038_monitoring_traces.sql
--
-- Foundation for distributed tracing (frontend->backend incident chains) and
-- the error-type / platform / instrumentation-source dimensions. All additive
-- and nullable: existing rows stay valid, nothing needs backfilling, and the
-- beacon/UI that populate these columns ship in later PRs.

-- --- Trace identity on every captured signal --------------------------------
-- trace_id ties a signal to the user action (and the backend work) it belongs
-- to; span_id / parent_span_id place it in the call tree.
ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS platform TEXT;
ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS capture_mode TEXT;

ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS platform TEXT;
ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS capture_mode TEXT;
-- error_type is the dimension behind "choose what you want to see": exception,
-- failed_request, crash, anr (app-not-responding), csp_violation, reported.
ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS error_type TEXT;

ALTER TABLE portfolio_web_vitals ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE portfolio_web_vitals ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE portfolio_web_vitals ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
ALTER TABLE portfolio_web_vitals ADD COLUMN IF NOT EXISTS platform TEXT;

ALTER TABLE portfolio_logs ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE portfolio_logs ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE portfolio_logs ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
ALTER TABLE portfolio_logs ADD COLUMN IF NOT EXISTS platform TEXT;

-- error_type also lives on the group so the issues list can facet by it.
ALTER TABLE portfolio_error_groups ADD COLUMN IF NOT EXISTS error_type TEXT;

-- capture_mode is closed (auto = browser/agent instrumentation, manual = the
-- developer's own tagging); platform and error_type stay open so the mobile
-- phase can add values without a migration.
ALTER TABLE portfolio_usage_events
    ADD CONSTRAINT portfolio_usage_events_capture_mode_check
    CHECK (capture_mode IS NULL OR capture_mode IN ('auto', 'manual')) NOT VALID;
ALTER TABLE portfolio_error_events
    ADD CONSTRAINT portfolio_error_events_capture_mode_check
    CHECK (capture_mode IS NULL OR capture_mode IN ('auto', 'manual')) NOT VALID;

-- Dimensions are always scoped to a product + time window, so the composite
-- (pipeline_id, <dimension>) is the useful index shape.
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_trace ON portfolio_usage_events(pipeline_id, trace_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_trace ON portfolio_error_events(pipeline_id, trace_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_type ON portfolio_error_events(pipeline_id, error_type);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_platform ON portfolio_usage_events(pipeline_id, platform);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_platform ON portfolio_error_events(pipeline_id, platform);

-- --- Spans: the trace store --------------------------------------------------
-- One row per unit of work (a page load, a fetch, a backend request, a slow
-- query, a manually tagged feature). Joined into traces by trace_id and into a
-- call tree by parent_span_id. Errors/events reference the same trace_id.
CREATE TABLE IF NOT EXISTS portfolio_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    source_id UUID REFERENCES portfolio_usage_sources(id) ON DELETE SET NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'client',
    service TEXT,
    feature TEXT,
    platform TEXT,
    capture_mode TEXT,
    status TEXT,
    release TEXT,
    environment TEXT,
    visitor_id TEXT,
    session_id TEXT,
    user_ref TEXT,
    attributes JSONB,
    start_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms DOUBLE PRECISION,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_spans_kind_check CHECK (kind IN ('client', 'server', 'internal')),
    CONSTRAINT portfolio_spans_capture_mode_check CHECK (capture_mode IS NULL OR capture_mode IN ('auto', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_spans_trace ON portfolio_spans(pipeline_id, trace_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_spans_started ON portfolio_spans(pipeline_id, start_at);
CREATE INDEX IF NOT EXISTS idx_portfolio_spans_feature ON portfolio_spans(pipeline_id, feature);
