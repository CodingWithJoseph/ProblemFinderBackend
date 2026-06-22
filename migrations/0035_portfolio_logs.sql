-- 0035_portfolio_logs.sql
--
-- Client log capture: a faceted log stream (debug/info/warn/error) emitted by
-- the beacon's log() call and optional console hook. Mirrors the usage/error
-- envelope so logs join to the same visitor/session/release/environment.

CREATE TABLE IF NOT EXISTS portfolio_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    source_id UUID REFERENCES portfolio_usage_sources(id) ON DELETE SET NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata JSONB,
    url TEXT,
    visitor_id TEXT,
    session_id TEXT,
    user_ref TEXT,
    release TEXT,
    environment TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_logs_level_check CHECK (level IN ('debug', 'info', 'warn', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_logs_pipeline_occurred
    ON portfolio_logs(pipeline_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_portfolio_logs_pipeline_level
    ON portfolio_logs(pipeline_id, level);
CREATE INDEX IF NOT EXISTS idx_portfolio_logs_pipeline_session
    ON portfolio_logs(pipeline_id, session_id);
