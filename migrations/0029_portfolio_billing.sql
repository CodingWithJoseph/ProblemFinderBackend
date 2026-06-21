-- 0029_portfolio_billing.sql
--
-- Invoice/event-derived revenue engine (PR1). Replaces the "sum active
-- subscriptions" MRR approximation with an append-only billing ledger plus a
-- per-customer MRR time series and derived movements. The old snapshot columns
-- on portfolio_revenue_sources are kept and still populated in parallel; this
-- migration only *adds* the new tables and the parallel invoice-engine columns
-- so totals can be compared before the cutover flag is flipped.
--
-- Idempotent by convention: CREATE TABLE / CREATE INDEX / ADD COLUMN all use
-- IF NOT EXISTS so re-applying against local Postgres or Supabase is safe.

-- 1. Immutable, append-only ledger. One row per recurring-revenue-affecting
--    Stripe fact, derived from invoices + invoice line items. Non-subscription
--    line items (one-off charges, setup fees) are excluded at ingestion time.
--    stripe_line_item_id is the natural idempotency key: re-running the
--    backfill never duplicates a row.
CREATE TABLE IF NOT EXISTS portfolio_billing_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revenue_source_id UUID NOT NULL REFERENCES portfolio_revenue_sources(id) ON DELETE CASCADE,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    stripe_invoice_id TEXT,
    stripe_line_item_id TEXT NOT NULL,
    -- This line's normalized monthly amount, after discounts, in cents.
    mrr_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'usd',
    -- Line item period.start: when this recurring level takes effect.
    effective_at TIMESTAMPTZ,
    is_proration BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_billing_events_line_item
    ON portfolio_billing_events(stripe_line_item_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_billing_events_source_customer
    ON portfolio_billing_events(revenue_source_id, stripe_customer_id, effective_at);

-- 2. Per-customer normalized MRR time series. Summing each customer's latest
--    row <= a date gives total MRR at that date. Granularity is invoice period
--    boundaries for backfilled history plus a sync-cadence point going forward.
CREATE TABLE IF NOT EXISTS portfolio_customer_mrr (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revenue_source_id UUID NOT NULL REFERENCES portfolio_revenue_sources(id) ON DELETE CASCADE,
    stripe_customer_id TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    mrr_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'usd',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_customer_mrr_unique
    ON portfolio_customer_mrr(revenue_source_id, stripe_customer_id, as_of_date);
CREATE INDEX IF NOT EXISTS idx_portfolio_customer_mrr_source_date
    ON portfolio_customer_mrr(revenue_source_id, as_of_date);

-- 3. Movements, derived by diffing consecutive portfolio_customer_mrr rows per
--    customer. At most one movement per customer per effective_date.
CREATE TABLE IF NOT EXISTS portfolio_mrr_movements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revenue_source_id UUID NOT NULL REFERENCES portfolio_revenue_sources(id) ON DELETE CASCADE,
    stripe_customer_id TEXT NOT NULL,
    effective_date DATE NOT NULL,
    movement_type TEXT NOT NULL,
    mrr_delta_cents INTEGER NOT NULL DEFAULT 0,
    mrr_after_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'usd',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_mrr_movements_type_check
        CHECK (movement_type IN ('new', 'expansion', 'contraction', 'churn', 'reactivation'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_mrr_movements_unique
    ON portfolio_mrr_movements(revenue_source_id, stripe_customer_id, effective_date);
CREATE INDEX IF NOT EXISTS idx_portfolio_mrr_movements_source_date
    ON portfolio_mrr_movements(revenue_source_id, effective_date);

-- 4. Parallel invoice-engine totals on the revenue source. The old snapshot
--    columns (current_mrr_cents / previous_mrr_cents / churn fields) stay and
--    are still written by the old path; these hold the new engine's numbers so
--    the serializer can read whichever the revenue_engine flag selects.
ALTER TABLE portfolio_revenue_sources
    ADD COLUMN IF NOT EXISTS invoice_mrr_cents INTEGER,
    ADD COLUMN IF NOT EXISTS previous_invoice_mrr_cents INTEGER,
    ADD COLUMN IF NOT EXISTS invoice_revenue_snapshot JSONB;
