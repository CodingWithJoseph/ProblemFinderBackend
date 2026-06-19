-- 0022_portfolio_errors.sql
--
-- First-party error monitoring for launched portfolio products. Mirrors the
-- usage pipeline (same portfolio_usage_sources public_key / domain guardrails),
-- adding grouped "issues" so the dashboard shows distinct problems rather than
-- a raw firehose of occurrences.

CREATE TABLE IF NOT EXISTS portfolio_error_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    source_id UUID REFERENCES portfolio_usage_sources(id) ON DELETE SET NULL,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'error',
    status TEXT NOT NULL DEFAULT 'unresolved',
    event_count INTEGER NOT NULL DEFAULT 0,
    last_release TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_error_groups_level_check CHECK (level IN ('error', 'warning')),
    CONSTRAINT portfolio_error_groups_status_check CHECK (status IN ('unresolved', 'resolved', 'ignored'))
);

-- One group per (product, fingerprint): the ingest path upserts on this.
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_error_groups_pipeline_fingerprint
    ON portfolio_error_groups(pipeline_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_groups_pipeline_id ON portfolio_error_groups(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_groups_last_seen_at ON portfolio_error_groups(last_seen_at);

CREATE TABLE IF NOT EXISTS portfolio_error_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    source_id UUID REFERENCES portfolio_usage_sources(id) ON DELETE SET NULL,
    group_id UUID REFERENCES portfolio_error_groups(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    message TEXT NOT NULL,
    stack TEXT,
    level TEXT NOT NULL DEFAULT 'error',
    handled BOOLEAN,
    url TEXT,
    release TEXT,
    visitor_id TEXT,
    session_id TEXT,
    user_ref TEXT,
    metadata JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_error_events_level_check CHECK (level IN ('error', 'warning'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_pipeline_id ON portfolio_error_events(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_group_id ON portfolio_error_events(group_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_occurred_at ON portfolio_error_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_portfolio_error_events_session_id ON portfolio_error_events(session_id);
