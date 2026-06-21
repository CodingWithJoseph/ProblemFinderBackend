-- 0032_portfolio_identities.sql
--
-- Identity join (PR3): connect a usage actor (user_ref, set via the tracker's
-- identify()) to a paying Stripe customer. The join key is an explicit
-- stripe_customer_id trait when present, with email as the fallback. This is the
-- bridge the usage<->revenue correlation (D1) reads across. Idempotent.

-- Stripe customers seen on the connected/own account, keyed by stripe_customer_id
-- per revenue source. Populated from the Stripe customer object during sync so
-- the email fallback has something to match against.
CREATE TABLE IF NOT EXISTS portfolio_customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revenue_source_id UUID NOT NULL REFERENCES portfolio_revenue_sources(id) ON DELETE CASCADE,
    stripe_customer_id TEXT NOT NULL,
    email TEXT,
    name TEXT,
    raw JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_customers_unique
    ON portfolio_customers(revenue_source_id, stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_customers_email
    ON portfolio_customers(revenue_source_id, lower(email));

-- One row per identified usage actor per product. resolution_method records how
-- the stripe_customer_id was determined: 'explicit' (id trait), 'email' (matched
-- a Stripe customer email), or 'unresolved' (no link yet).
CREATE TABLE IF NOT EXISTS portfolio_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    user_ref TEXT NOT NULL,
    stripe_customer_id TEXT,
    email TEXT,
    group_id TEXT,
    traits JSONB,
    resolution_method TEXT NOT NULL DEFAULT 'unresolved',
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_identities_method_check
        CHECK (resolution_method IN ('explicit', 'email', 'unresolved'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_identities_unique
    ON portfolio_identities(pipeline_id, user_ref);
CREATE INDEX IF NOT EXISTS idx_portfolio_identities_customer
    ON portfolio_identities(pipeline_id, stripe_customer_id);
