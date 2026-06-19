-- 0023_portfolio_alerts.sql
--
-- Alerting: a dedupe log so the same condition doesn't email repeatedly, plus
-- previous-MRR tracking on revenue sources so a drop can be detected.

ALTER TABLE portfolio_revenue_sources
    ADD COLUMN IF NOT EXISTS previous_mrr_cents INTEGER;

CREATE TABLE IF NOT EXISTS portfolio_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    recipient TEXT,
    channel TEXT NOT NULL DEFAULT 'email',
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One alert per (product, type, key): the evaluator checks this before sending.
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_alerts_dedupe
    ON portfolio_alerts(pipeline_id, alert_type, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_portfolio_alerts_pipeline_id ON portfolio_alerts(pipeline_id);
