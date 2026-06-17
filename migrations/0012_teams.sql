-- 0012_teams.sql
--
-- Foundational collaboration tables. Invitations are represented as
-- team_members rows with status = 'invited' and nullable user_id.

CREATE TABLE IF NOT EXISTS teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS team_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id TEXT,
    email TEXT,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT team_members_role_check CHECK (role IN ('owner', 'admin', 'member')),
    CONSTRAINT team_members_status_check CHECK (status IN ('active', 'invited', 'removed'))
);

CREATE INDEX IF NOT EXISTS idx_teams_owner_user_id ON teams(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_team_members_team_id ON team_members(team_id);
CREATE INDEX IF NOT EXISTS idx_team_members_user_id ON team_members(user_id);
CREATE INDEX IF NOT EXISTS idx_team_members_email ON team_members(lower(email));

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_team_user_unique
    ON team_members(team_id, user_id)
    WHERE user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_team_email_unique
    ON team_members(team_id, lower(email))
    WHERE email IS NOT NULL;
