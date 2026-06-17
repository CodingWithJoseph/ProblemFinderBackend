-- 0013_issues.sql
--
-- Opportunity-development Issues. Issues are distinct from prototype/build
-- Tasks and can belong to a user, team, pipeline card, and optionally another
-- issue as a sub-issue.

CREATE TABLE IF NOT EXISTS issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    team_id UUID REFERENCES teams(id) ON DELETE SET NULL,
    pipeline_id UUID REFERENCES pipeline(id) ON DELETE SET NULL,
    parent_issue_id UUID REFERENCES issues(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    position INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    CONSTRAINT issues_status_check CHECK (status IN ('open', 'done', 'archived'))
);

CREATE TABLE IF NOT EXISTS issue_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_id UUID NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_issues_user_id ON issues(user_id);
CREATE INDEX IF NOT EXISTS idx_issues_team_id ON issues(team_id);
CREATE INDEX IF NOT EXISTS idx_issues_pipeline_id ON issues(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_issues_parent_issue_id ON issues(parent_issue_id);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issue_comments_issue_id ON issue_comments(issue_id);
CREATE INDEX IF NOT EXISTS idx_issue_comments_user_id ON issue_comments(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_pipeline_default_source_unique
    ON issues(pipeline_id, source)
    WHERE pipeline_id IS NOT NULL
      AND source IN ('analyze_signals', 'validate_breakdown_problems', 'create_tasks');
