-- 0042_goals.sql
--
-- Static, admin-defined goal/achievement system. There is no user-facing config:
-- goal_definitions + goal_tiers are seeded here and never edited by end users.
-- The Goal Progress Engine (app/services/goals.py) reads existing rollups/flags,
-- compares the current value to the next un-achieved tier per project+goal or
-- account+goal, and inserts an achievement row when a tier is crossed. The
-- achievement tables double as the append-only milestone log.
--
-- Purely additive: no prior goals schema exists, so there is nothing to migrate
-- or drop. All statements are IF NOT EXISTS / ON CONFLICT DO NOTHING so the file
-- is safe to re-apply.

-- 1. Definitions. `id` is a stable slug so seeds are idempotent and the engine
--    can map metric_key → a rollup. `scope` splits project- vs account-level
--    goals (the 4-column spec omitted it, but the two achievement tables require
--    it). `icon` is a lucide icon name rendered by the UI (no badge collectibles).
CREATE TABLE IF NOT EXISTS goal_definitions (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    icon TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT goal_definitions_scope_check CHECK (scope IN ('project', 'account'))
);

-- 2. Ordered tiers per goal. threshold_value is compared to the goal's current
--    metric value; label is the human tier name (e.g. "$1k", "3/4", "500").
CREATE TABLE IF NOT EXISTS goal_tiers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal_definition_id TEXT NOT NULL REFERENCES goal_definitions(id) ON DELETE CASCADE,
    tier_index INTEGER NOT NULL,
    threshold_value BIGINT NOT NULL,
    label TEXT NOT NULL,
    UNIQUE (goal_definition_id, tier_index)
);
CREATE INDEX IF NOT EXISTS idx_goal_tiers_definition ON goal_tiers(goal_definition_id, tier_index);

-- 3. Project-scoped achievements (one row per crossed tier). Append-only.
CREATE TABLE IF NOT EXISTS project_goal_achievements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    goal_definition_id TEXT NOT NULL REFERENCES goal_definitions(id) ON DELETE CASCADE,
    tier_index INTEGER NOT NULL,
    achieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, goal_definition_id, tier_index)
);
CREATE INDEX IF NOT EXISTS idx_project_goal_achievements_project ON project_goal_achievements(project_id, achieved_at);

-- 4. Account-scoped achievements (portfolio-level; keyed by user, no project_id).
CREATE TABLE IF NOT EXISTS account_goal_achievements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    goal_definition_id TEXT NOT NULL REFERENCES goal_definitions(id) ON DELETE CASCADE,
    tier_index INTEGER NOT NULL,
    achieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, goal_definition_id, tier_index)
);
CREATE INDEX IF NOT EXISTS idx_account_goal_achievements_user ON account_goal_achievements(user_id, achieved_at);

-- ── Seed definitions ─────────────────────────────────────────────────────────
INSERT INTO goal_definitions (id, scope, category, title, metric_key, icon, sort_order) VALUES
    ('proj_signups', 'project', 'growth',   'Signups',        'signups',          'user-plus',    10),
    ('proj_revenue', 'project', 'revenue',  'Revenue',        'mrr_cents',        'dollar-sign',  20),
    ('proj_traffic', 'project', 'traffic',  'Traffic',        'traffic',          'trending-up',  30),
    ('proj_setup',   'project', 'setup',    'Product setup',  'setup_steps',      'plug',         40),
    ('proj_issues',  'project', 'issues',   'Issue tracking', 'issues_created',   'circle-alert', 50),
    ('acct_launches','account', 'portfolio','Launch products','products_launched','rocket',       10),
    ('acct_problems','account', 'discovery','Problem discovery','problems_created','lightbulb',    20),
    ('acct_tasks',   'account', 'execution','Task management','tasks_created',    'square-check',  30),
    ('acct_team',    'account', 'team',     'Team',           'teammates_invited','users',         40)
ON CONFLICT (id) DO NOTHING;

-- ── Seed tiers ───────────────────────────────────────────────────────────────
INSERT INTO goal_tiers (goal_definition_id, tier_index, threshold_value, label) VALUES
    -- Signups: 5, 10, 50, 100, 500, 1000
    ('proj_signups', 0, 5, '5'), ('proj_signups', 1, 10, '10'), ('proj_signups', 2, 50, '50'),
    ('proj_signups', 3, 100, '100'), ('proj_signups', 4, 500, '500'), ('proj_signups', 5, 1000, '1000'),
    -- Revenue (MRR, cents): first $, $100, $1k, $10k
    ('proj_revenue', 0, 1, 'First $'), ('proj_revenue', 1, 10000, '$100'),
    ('proj_revenue', 2, 100000, '$1k'), ('proj_revenue', 3, 1000000, '$10k'),
    -- Traffic (pageviews): 100, 1k, 10k, 100k
    ('proj_traffic', 0, 100, '100'), ('proj_traffic', 1, 1000, '1k'),
    ('proj_traffic', 2, 10000, '10k'), ('proj_traffic', 3, 100000, '100k'),
    -- Product setup (of 4 connections): 1/4 .. 4/4
    ('proj_setup', 0, 1, '1/4'), ('proj_setup', 1, 2, '2/4'),
    ('proj_setup', 2, 3, '3/4'), ('proj_setup', 3, 4, '4/4'),
    -- Issue tracking: 1, 5, 20
    ('proj_issues', 0, 1, '1'), ('proj_issues', 1, 5, '5'), ('proj_issues', 2, 20, '20'),
    -- Launch products: 1, 5, 10, 25
    ('acct_launches', 0, 1, '1'), ('acct_launches', 1, 5, '5'),
    ('acct_launches', 2, 10, '10'), ('acct_launches', 3, 25, '25'),
    -- Problem discovery: 1, 20, 100, 500
    ('acct_problems', 0, 1, '1'), ('acct_problems', 1, 20, '20'),
    ('acct_problems', 2, 100, '100'), ('acct_problems', 3, 500, '500'),
    -- Task management: 1, 20, 100
    ('acct_tasks', 0, 1, '1'), ('acct_tasks', 1, 20, '20'), ('acct_tasks', 2, 100, '100'),
    -- Team: 1, 5
    ('acct_team', 0, 1, '1'), ('acct_team', 1, 5, '5')
ON CONFLICT (goal_definition_id, tier_index) DO NOTHING;
