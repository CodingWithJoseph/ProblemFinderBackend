-- 0018_usage_source_domain.sql
--
-- Guided setup metadata and domain guardrails for portfolio usage sources.

ALTER TABLE portfolio_usage_sources
    ADD COLUMN IF NOT EXISTS product_url TEXT,
    ADD COLUMN IF NOT EXISTS allowed_domain TEXT;

CREATE INDEX IF NOT EXISTS idx_portfolio_usage_sources_allowed_domain
    ON portfolio_usage_sources(allowed_domain);
