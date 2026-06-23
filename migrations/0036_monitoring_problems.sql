-- 0036_monitoring_problems.sql
--
-- Monitoring problems: a durable record the alert evaluator writes *before* it
-- emails, so every fired condition becomes an inspectable object (not just an
-- email). DISTINCT from the opportunity-discovery `problems` table — this is the
-- monitoring domain. detected_at + metric/baseline/observed feed the impact view
-- (before/during/after).

CREATE TABLE IF NOT EXISTS monitoring_problems (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    severity TEXT NOT NULL DEFAULT 'warning',
    status TEXT NOT NULL DEFAULT 'open',
    metric TEXT,
    baseline DOUBLE PRECISION,
    observed DOUBLE PRECISION,
    metadata JSONB,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT monitoring_problems_status_check CHECK (status IN ('open', 'resolved')),
    CONSTRAINT monitoring_problems_severity_check CHECK (severity IN ('info', 'warning', 'critical'))
);

-- One problem per (product, kind, dedupe_key): mirrors the alert dedupe so a
-- condition is recorded once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitoring_problems_unique
    ON monitoring_problems(pipeline_id, kind, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_monitoring_problems_pipeline_detected
    ON monitoring_problems(pipeline_id, detected_at);
