-- 0046_search_sessions.sql
--
-- Lightweight persistence for conversational Search. Sessions retain validated
-- interpretation drafts and result identifiers only; existing cluster rows remain
-- the authoritative result/evidence store. Unsaved sessions receive an expires_at
-- value and are removed opportunistically by the session API.

CREATE TABLE IF NOT EXISTS search_sessions (
    id UUID PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL,
    title VARCHAR(160) NOT NULL DEFAULT 'New search',
    saved_at TIMESTAMPTZ NULL,
    archived_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NULL,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_search_sessions_user_activity
    ON search_sessions (user_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS ix_search_sessions_expires_at
    ON search_sessions (expires_at)
    WHERE saved_at IS NULL;

CREATE TABLE IF NOT EXISTS search_turns (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
    user_message TEXT NOT NULL,
    interpretation JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_search_turns_session_created
    ON search_turns (session_id, created_at);

CREATE TABLE IF NOT EXISTS search_runs (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
    draft JSONB NOT NULL,
    result_cluster_ids TEXT[] NOT NULL DEFAULT '{}',
    result_count INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_search_runs_session_created
    ON search_runs (session_id, created_at);
