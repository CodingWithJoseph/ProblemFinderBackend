-- 0024_portfolio_alert_settings.sql
--
-- Per-product alert preferences. One row per pipeline; absence means all
-- triggers on with the global default thresholds. Threshold columns are
-- nullable and fall back to config defaults when null.

CREATE TABLE IF NOT EXISTS portfolio_alert_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL UNIQUE REFERENCES pipeline(id) ON DELETE CASCADE,
    new_issue_enabled BOOLEAN NOT NULL DEFAULT true,
    error_spike_enabled BOOLEAN NOT NULL DEFAULT true,
    signups_drop_enabled BOOLEAN NOT NULL DEFAULT true,
    revenue_drop_enabled BOOLEAN NOT NULL DEFAULT true,
    error_spike_multiplier DOUBLE PRECISION,
    signups_drop_pct DOUBLE PRECISION,
    revenue_drop_pct DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portfolio_alert_settings_pipeline_id ON portfolio_alert_settings(pipeline_id);
