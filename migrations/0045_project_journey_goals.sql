-- 0045_project_journey_goals.sql
--
-- Adds pre-launch *journey* goals at project scope: the path a project takes
-- toward launch (define problems -> define features -> build features), broken
-- into 5/10/15/20 tiers. Until now every project goal was a post-launch outcome
-- (signups, revenue, traffic) hidden behind a hard launch gate, so a project in
-- Discovery had no goals to work toward. These fill that gap.
--
-- A new `requires_launch` flag on goal_definitions splits the two kinds: journey
-- goals show pre-launch (requires_launch = false); the existing outcome goals
-- stay post-launch (requires_launch = true, the default). The goals endpoint
-- filters by the project's launch state.
--
-- Additive and idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING).

-- 1. Launch-gating flag. Default true so the existing outcome goals keep their
--    post-launch behaviour without an explicit backfill.
ALTER TABLE goal_definitions ADD COLUMN IF NOT EXISTS requires_launch BOOLEAN NOT NULL DEFAULT true;

-- 2. Journey goal definitions (project scope, pre-launch). sort_order places them
--    ahead of the outcome goals so the path to launch reads first.
INSERT INTO goal_definitions (id, scope, category, title, metric_key, icon, sort_order, requires_launch) VALUES
    ('proj_problems', 'project', 'discovery', 'Problems defined', 'problems_defined', 'lightbulb',   4, false),
    ('proj_features', 'project', 'build',     'Features defined', 'features_defined', 'list-checks', 6, false),
    ('proj_built',    'project', 'build',     'Features built',   'features_built',   'hammer',      8, false)
ON CONFLICT (id) DO NOTHING;

-- 3. Tiers: 5, 10, 15, 20 for each. estimate_days left NULL so the engine's
--    scale-aware fallback applies (these are close, early-stage steps).
INSERT INTO goal_tiers (goal_definition_id, tier_index, threshold_value, label) VALUES
    ('proj_problems', 0, 5, '5'), ('proj_problems', 1, 10, '10'), ('proj_problems', 2, 15, '15'), ('proj_problems', 3, 20, '20'),
    ('proj_features', 0, 5, '5'), ('proj_features', 1, 10, '10'), ('proj_features', 2, 15, '15'), ('proj_features', 3, 20, '20'),
    ('proj_built',    0, 5, '5'), ('proj_built',    1, 10, '10'), ('proj_built',    2, 15, '15'), ('proj_built',    3, 20, '20')
ON CONFLICT (goal_definition_id, tier_index) DO NOTHING;
