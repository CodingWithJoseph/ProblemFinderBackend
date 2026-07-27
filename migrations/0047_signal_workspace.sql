-- 0047_signal_workspace.sql
--
-- Versioned, citation-safe analysis storage for the three-screen Signal
-- workspace. Existing cluster_signals rows remain the source-metrics store.
-- These tables contain user-owned analysis cases, immutable generated versions,
-- non-destructive user overrides, background jobs and grounded conversations.

CREATE TABLE IF NOT EXISTS signal_analysis_cases (
    id UUID PRIMARY KEY,
    pipeline_id UUID NOT NULL UNIQUE REFERENCES pipeline(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued',
            'generating',
            'ready',
            'stale',
            'insufficient_evidence',
            'failed'
        )),
    progress_step VARCHAR(48) NULL,
    progress_label VARCHAR(160) NULL,
    safe_error TEXT NULL,
    source_fingerprint TEXT NULL,
    source_updated_at TIMESTAMPTZ NULL,
    analyzed_at TIMESTAMPTZ NULL,
    current_version_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_signal_analysis_cases_user_updated
    ON signal_analysis_cases (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS signal_analysis_versions (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES signal_analysis_cases(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    schema_version VARCHAR(48) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    model VARCHAR(160) NOT NULL,
    source_fingerprint TEXT NOT NULL,
    analysis JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, version)
);

CREATE INDEX IF NOT EXISTS ix_signal_analysis_versions_case_generated
    ON signal_analysis_versions (case_id, generated_at DESC);

ALTER TABLE signal_analysis_cases
    ADD CONSTRAINT fk_signal_analysis_cases_current_version
    FOREIGN KEY (current_version_id)
    REFERENCES signal_analysis_versions(id)
    ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS signal_case_overrides (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES signal_analysis_cases(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    object_kind VARCHAR(48) NOT NULL,
    object_id VARCHAR(160) NOT NULL,
    patch JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, object_kind, object_id)
);

CREATE INDEX IF NOT EXISTS ix_signal_case_overrides_case
    ON signal_case_overrides (case_id);

CREATE TABLE IF NOT EXISTS signal_analysis_jobs (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES signal_analysis_cases(id) ON DELETE CASCADE,
    requested_by VARCHAR(128) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('initial', 'refresh')),
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts > 0),
    source_fingerprint TEXT NULL,
    lease_owner VARCHAR(160) NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    error_category VARCHAR(64) NULL,
    safe_error TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_signal_analysis_jobs_status_created
    ON signal_analysis_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS ix_signal_analysis_jobs_case_created
    ON signal_analysis_jobs (case_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_analysis_jobs_active_case
    ON signal_analysis_jobs (case_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS signal_conversations (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES signal_analysis_cases(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    title VARCHAR(160) NOT NULL DEFAULT 'Signal conversation',
    archived_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_signal_conversations_case_updated
    ON signal_conversations (case_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS signal_conversation_turns (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES signal_conversations(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    citations JSONB NOT NULL DEFAULT '[]'::JSONB,
    proposal JSONB NULL,
    insufficient_evidence BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_signal_conversation_turns_conversation_created
    ON signal_conversation_turns (conversation_id, created_at);

