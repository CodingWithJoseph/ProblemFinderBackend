-- 0050_solution_warroom.sql
-- Versioned, traceable solution plans generated from an approved problem case.

CREATE TABLE IF NOT EXISTS solution_cases (
    id UUID PRIMARY KEY,
    pipeline_id UUID NOT NULL UNIQUE REFERENCES pipeline(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'generating', 'ready', 'failed')),
    progress_step VARCHAR(48) NULL,
    progress_label VARCHAR(160) NULL,
    safe_error TEXT NULL,
    problem_case_version INTEGER NOT NULL DEFAULT 0 CHECK (problem_case_version >= 0),
    request JSONB NOT NULL DEFAULT '{}'::JSONB,
    current_version_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_solution_cases_user_updated
    ON solution_cases (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS solution_versions (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES solution_cases(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    schema_version VARCHAR(48) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    model VARCHAR(160) NOT NULL,
    problem_case_version INTEGER NOT NULL CHECK (problem_case_version > 0),
    document JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, version)
);

ALTER TABLE solution_cases
    ADD CONSTRAINT fk_solution_cases_current_version
    FOREIGN KEY (current_version_id)
    REFERENCES solution_versions(id)
    ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS solution_jobs (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES solution_cases(id) ON DELETE CASCADE,
    requested_by VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts > 0),
    lease_owner VARCHAR(160) NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    error_category VARCHAR(64) NULL,
    safe_error TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_solution_jobs_status_created
    ON solution_jobs (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_solution_jobs_active_case
    ON solution_jobs (case_id)
    WHERE status IN ('queued', 'running');
