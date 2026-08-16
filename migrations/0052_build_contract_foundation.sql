-- 0052_build_contract_foundation.sql
-- Approval pins coding-agent access to an exact immutable Build Contract version.

ALTER TABLE solution_cases
    ADD COLUMN IF NOT EXISTS approval_status VARCHAR(24) NOT NULL DEFAULT 'draft'
        CHECK (approval_status IN ('draft', 'approved')),
    ADD COLUMN IF NOT EXISTS approved_version_id UUID NULL
        REFERENCES solution_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL;

ALTER TABLE solution_task_states
    ADD COLUMN IF NOT EXISTS verification JSONB NOT NULL DEFAULT '{}'::jsonb;
