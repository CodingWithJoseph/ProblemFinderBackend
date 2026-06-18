-- 0016_issue_assignee.sql
--
-- Optional issue owner within the issue's team. The application validates that
-- assignee_id belongs to the same team as the issue before writing it.

ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS assignee_id UUID REFERENCES team_members(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_issues_assignee_id ON issues(assignee_id);
