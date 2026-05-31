CREATE TABLE IF NOT EXISTS user_flags (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL,
    opportunity_id uuid NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    pipeline_id uuid NULL REFERENCES pipeline(id) ON DELETE SET NULL,
    reason text NOT NULL,
    note text NULL,
    hidden boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_flags_user_opportunity UNIQUE (user_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS idx_user_flags_user_id ON user_flags(user_id);
CREATE INDEX IF NOT EXISTS idx_user_flags_opportunity_id ON user_flags(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_user_flags_pipeline_id ON user_flags(pipeline_id);
