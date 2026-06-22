-- 0034_portfolio_web_vitals.sql
--
-- Experience vitals: Core Web Vitals (LCP, CLS, INP, FCP, TTFB) captured by the
-- beacon, one row per metric sample. The per-URL rollups (p75 + rating) read off
-- this table. Mirrors the usage/error pipeline's source + envelope columns so
-- vitals join to the same visitor/session/release/environment.

CREATE TABLE IF NOT EXISTS portfolio_web_vitals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    source_id UUID REFERENCES portfolio_usage_sources(id) ON DELETE SET NULL,
    metric TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    rating TEXT,
    url TEXT,
    navigation_id TEXT,
    visitor_id TEXT,
    session_id TEXT,
    user_ref TEXT,
    release TEXT,
    environment TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_web_vitals_metric_check CHECK (metric IN ('LCP', 'CLS', 'INP', 'FCP', 'TTFB')),
    CONSTRAINT portfolio_web_vitals_rating_check CHECK (rating IS NULL OR rating IN ('good', 'needs-improvement', 'poor'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_web_vitals_pipeline_occurred
    ON portfolio_web_vitals(pipeline_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_portfolio_web_vitals_pipeline_metric
    ON portfolio_web_vitals(pipeline_id, metric);
CREATE INDEX IF NOT EXISTS idx_portfolio_web_vitals_pipeline_url
    ON portfolio_web_vitals(pipeline_id, url);
