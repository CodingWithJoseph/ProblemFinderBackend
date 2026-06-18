-- 0019_portfolio_revenue.sql
--
-- Revenue connection placeholders for launched portfolio products.

CREATE TABLE IF NOT EXISTS portfolio_revenue_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'stripe',
    status TEXT NOT NULL DEFAULT 'not_connected',
    provider_account_label TEXT,
    current_mrr_cents INTEGER,
    new_customers_30d INTEGER,
    churned_customers_30d INTEGER,
    churn_rate_30d DOUBLE PRECISION,
    revenue_snapshot JSONB,
    connected_at TIMESTAMPTZ,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_revenue_sources_provider_check CHECK (provider IN ('stripe')),
    CONSTRAINT portfolio_revenue_sources_status_check CHECK (status IN ('not_connected', 'connected', 'needs_attention'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_revenue_sources_pipeline_provider
    ON portfolio_revenue_sources(pipeline_id, provider);
CREATE INDEX IF NOT EXISTS idx_portfolio_revenue_sources_user_id
    ON portfolio_revenue_sources(user_id);
