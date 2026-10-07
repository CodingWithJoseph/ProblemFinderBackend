-- Profiles and onboarding use the existing application PostgreSQL database.
-- Product user_id columns now contain verified Supabase Auth UUIDs.
-- No identity/data rewrite: this cutover has no existing users to migrate.
CREATE TABLE IF NOT EXISTS account_profiles (
    id VARCHAR(128) PRIMARY KEY,
    display_name VARCHAR(160) NOT NULL DEFAULT '',
    onboarding_complete BOOLEAN NOT NULL DEFAULT FALSE,
    focus_areas JSONB NOT NULL DEFAULT '[]'::jsonb,
    deleted_at TIMESTAMPTZ,
    deletion_completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS launch_waitlist (
    id VARCHAR(64) PRIMARY KEY,
    email VARCHAR(320) NOT NULL,
    source VARCHAR(64) NOT NULL,
    consent_version VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
