-- 0053_mcp_oauth.sql
-- Browser authorization for account-scoped hosted MCP connections.

CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id VARCHAR(512) PRIMARY KEY,
    metadata JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcp_oauth_pending_authorizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id VARCHAR(512) NOT NULL REFERENCES mcp_oauth_clients(client_id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT TRUE,
    code_challenge TEXT NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    state TEXT NULL,
    resource TEXT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_mcp_oauth_pending_client
    ON mcp_oauth_pending_authorizations(client_id);

CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code_hash VARCHAR(64) NOT NULL UNIQUE,
    client_id VARCHAR(512) NOT NULL REFERENCES mcp_oauth_clients(client_id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT TRUE,
    code_challenge TEXT NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    resource TEXT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_mcp_oauth_codes_client ON mcp_oauth_codes(client_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_codes_user ON mcp_oauth_codes(user_id);

CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    token_type VARCHAR(16) NOT NULL CHECK (token_type IN ('access', 'refresh')),
    token_prefix VARCHAR(24) NOT NULL,
    client_id VARCHAR(512) NOT NULL REFERENCES mcp_oauth_clients(client_id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    resource TEXT NULL,
    selected_pipeline_id UUID NULL REFERENCES pipeline(id) ON DELETE SET NULL,
    family_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL,
    last_used_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_mcp_oauth_tokens_client ON mcp_oauth_tokens(client_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_tokens_user ON mcp_oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_tokens_family ON mcp_oauth_tokens(family_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_tokens_project ON mcp_oauth_tokens(selected_pipeline_id);
