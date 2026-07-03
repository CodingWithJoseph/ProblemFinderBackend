-- 0041_portfolio_revenue_daily.sql
--
-- Daily gross revenue series from Stripe charges. The existing revenue tables
-- (portfolio_customer_mrr / portfolio_billing_events) model *recurring* revenue
-- (MRR), which is a snapshot that barely moves day to day. The Portfolio
-- overview card needs true daily financial activity — how much money actually
-- came in each day — so it reads like the traffic / usage / errors cards with a
-- prior-7-days comparison. This table holds that series: one row per
-- (revenue source, day, currency), gross succeeded-charge amount in cents.
--
-- Idempotent by convention: the sync upserts on the unique key below, so
-- re-running a sync for an overlapping window never duplicates a day.

CREATE TABLE IF NOT EXISTS portfolio_revenue_daily (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revenue_source_id UUID NOT NULL REFERENCES portfolio_revenue_sources(id) ON DELETE CASCADE,
    -- The calendar day (UTC) the charges settled on.
    as_of_date DATE NOT NULL,
    -- Gross successful-charge amount for the day, in the charge currency's
    -- minor units (cents). Refunds are not netted out — this is gross revenue.
    gross_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'usd',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_revenue_daily_unique
    ON portfolio_revenue_daily(revenue_source_id, as_of_date, currency);
CREATE INDEX IF NOT EXISTS idx_portfolio_revenue_daily_source_date
    ON portfolio_revenue_daily(revenue_source_id, as_of_date);
