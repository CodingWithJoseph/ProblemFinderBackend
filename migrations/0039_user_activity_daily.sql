-- Daily authenticated-user activity for the workspace dashboard heatmap.
-- One atomic rollup row per user/day avoids storing a high-volume event log.

CREATE TABLE IF NOT EXISTS user_activity_daily (
    user_id VARCHAR(128) NOT NULL,
    activity_date DATE NOT NULL,
    login_count INTEGER NOT NULL DEFAULT 0,
    action_count INTEGER NOT NULL DEFAULT 0,
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, activity_date),
    CONSTRAINT user_activity_daily_login_count_check CHECK (login_count >= 0),
    CONSTRAINT user_activity_daily_action_count_check CHECK (action_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_user_activity_daily_date
    ON user_activity_daily(activity_date);
