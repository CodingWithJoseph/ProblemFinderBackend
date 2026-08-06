-- 0051_ide_mcp_access.sql
-- Revocable project-scoped IDE credentials and mutable task progress overlays.

CREATE TABLE IF NOT EXISTS ide_access_tokens (
    id UUID PRIMARY KEY,
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    name VARCHAR(120) NOT NULL,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    token_prefix VARCHAR(24) NOT NULL,
    last_used_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ide_access_tokens_project
    ON ide_access_tokens (pipeline_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS solution_task_states (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES solution_cases(id) ON DELETE CASCADE,
    task_id VARCHAR(160) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'blocked', 'done')),
    note TEXT NULL,
    updated_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, task_id)
);

CREATE INDEX IF NOT EXISTS ix_solution_task_states_case
    ON solution_task_states (case_id, updated_at DESC);
