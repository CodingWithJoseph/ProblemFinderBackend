-- 0033_portfolio_event_release_env.sql
--
-- Persist release + environment on usage events (error events already carry
-- release). The beacon has emitted both on every event since the SDK rebuild
-- (B.1); this is the column that finally stores them, unblocking
-- split-by-release and split-by-environment across usage data.

ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS release TEXT;
ALTER TABLE portfolio_usage_events ADD COLUMN IF NOT EXISTS environment TEXT;
ALTER TABLE portfolio_error_events ADD COLUMN IF NOT EXISTS environment TEXT;

-- Split-by dimensions are always scoped to a product and a time window, so the
-- composite (pipeline_id, <dimension>) is the useful shape.
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_release
    ON portfolio_usage_events(pipeline_id, release);
CREATE INDEX IF NOT EXISTS idx_portfolio_usage_events_environment
    ON portfolio_usage_events(pipeline_id, environment);
