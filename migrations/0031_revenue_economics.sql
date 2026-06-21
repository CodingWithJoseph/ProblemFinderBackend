-- 0031_revenue_economics.sql
--
-- Unit-economics inputs for the revenue metrics layer (PR2). Churn / NRR / GRR /
-- ARPA / quick-ratio are derived purely from the movement + customer-MRR tables
-- and need no input. LTV / CAC payback / Rule-of-40 need a gross margin and
-- (optionally) CAC + profit margin; these are stored per revenue source and,
-- when null, fall back to the configured default (and the metric is badged
-- "estimated"). Idempotent.

ALTER TABLE portfolio_revenue_sources
    -- 0..1 fraction. Null → use revenue_default_gross_margin_pct (estimated).
    ADD COLUMN IF NOT EXISTS gross_margin_pct DOUBLE PRECISION,
    -- Customer acquisition cost in cents. Null → CAC-based metrics omitted.
    ADD COLUMN IF NOT EXISTS cac_cents INTEGER,
    -- 0..1 fraction operating profit margin, an input to Rule of 40. Null →
    -- Rule of 40 is estimated/omitted.
    ADD COLUMN IF NOT EXISTS profit_margin_pct DOUBLE PRECISION;
