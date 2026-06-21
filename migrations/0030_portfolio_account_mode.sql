-- 0030_portfolio_account_mode.sql
--
-- First-party (own-key) revenue mode (PR1.5). PR1 assumed connected accounts
-- read via a Stripe Connect token + `stripe_account` param. Immensity can't
-- Connect to itself, so self-monitoring needs to read its *own* account directly
-- with STRIPE_SECRET_KEY and no `stripe_account`. This adds a discriminator;
-- the engine math (billing events → customer MRR → movements) is unchanged and
-- credential-agnostic.
--
-- Idempotent by convention. Existing rows backfill to 'connected' via the column
-- default, preserving the current Connect-based behavior.

ALTER TABLE portfolio_revenue_sources
    ADD COLUMN IF NOT EXISTS account_mode TEXT NOT NULL DEFAULT 'connected';

-- Constraint added separately + guarded so re-applying doesn't error on an
-- already-present constraint (ADD CONSTRAINT has no IF NOT EXISTS in PG16).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'portfolio_revenue_sources_account_mode_check'
    ) THEN
        ALTER TABLE portfolio_revenue_sources
            ADD CONSTRAINT portfolio_revenue_sources_account_mode_check
            CHECK (account_mode IN ('connected', 'first_party'));
    END IF;
END $$;
