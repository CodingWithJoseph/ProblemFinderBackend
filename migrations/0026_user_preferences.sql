-- 0026_user_preferences.sql
--
-- Per-user preferences: notification delivery (alert emails on/off, digest
-- cadence, optional alternate email) plus workspace defaults. One row per user;
-- absence means all defaults. ``last_digest_sent_at`` tracks the digest job so a
-- daily/weekly summary isn't sent twice.

CREATE TABLE IF NOT EXISTS user_preferences (
    uid TEXT PRIMARY KEY,
    alerts_email_enabled BOOLEAN NOT NULL DEFAULT true,
    digest_cadence TEXT NOT NULL DEFAULT 'instant',  -- instant | daily | weekly
    alert_email TEXT,
    default_pipeline_id TEXT,
    default_landing TEXT,
    last_digest_sent_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
