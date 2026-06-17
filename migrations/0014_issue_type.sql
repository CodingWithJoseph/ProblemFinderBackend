ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS issue_type varchar(50) NOT NULL DEFAULT 'issue';

CREATE INDEX IF NOT EXISTS idx_issues_issue_type ON issues(issue_type);
