-- 0017_portfolio_usage.sql
--
-- Lightweight first-party usage monitoring for launched portfolio products.

CREATE TABLE IF NOT EXISTS portfolio_usage_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT 'Website usage snippet',
    status TEXT NOT NULL DEFAULT 'connected',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    CONSTRAINT portfolio_usage_sources_status_check CHECK (status IN ('connected', 'paused', 'error'))
);

CREATE TABLE IF NOT EXISTS portfolio_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    source_id UUID REFERENCES portfolio_usage_sources(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    visitor_id TEXT,
    session_id TEXT,
    user_ref TEXT,
    url TEXT,
    referrer TEXT,
    metadata JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_usage_events_type_check CHECK (event_type IN ('pageview', 'signup', 'login', 'activation', 'custom'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_usage_sources_pipeline_id ON portfolio_usage_sources(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_sources_user_id ON portfolio_usage_sources(user_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_pipeline_id ON portfolio_usage_events(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_source_id ON portfolio_usage_events(source_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_type ON portfolio_usage_events(event_type);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_occurred_at ON portfolio_usage_events(occurred_at);
