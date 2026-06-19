-- 0021_team_invites.sql
--
-- Email-invite acceptance for team members. Invites previously created a
-- team_members row but had no way to be delivered or accepted; these columns
-- back the invite link (single-use token) and its expiry.

ALTER TABLE team_members
    ADD COLUMN IF NOT EXISTS invite_token TEXT,
    ADD COLUMN IF NOT EXISTS invite_token_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invited_at TIMESTAMPTZ;

-- Tokens are the secret in the invite URL, so they must be unique. Partial
-- index keeps the constraint off accepted/cleared rows (token IS NULL).
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_invite_token
    ON team_members(invite_token)
    WHERE invite_token IS NOT NULL;
