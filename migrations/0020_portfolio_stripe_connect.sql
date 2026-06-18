-- 0020_portfolio_stripe_connect.sql
--
-- Stripe Connect handshake metadata for portfolio revenue sources.

ALTER TABLE portfolio_revenue_sources
    ADD COLUMN IF NOT EXISTS provider_account_id TEXT,
    ADD COLUMN IF NOT EXISTS oauth_state TEXT,
    ADD COLUMN IF NOT EXISTS oauth_state_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_portfolio_revenue_sources_oauth_state
    ON portfolio_revenue_sources(oauth_state);
