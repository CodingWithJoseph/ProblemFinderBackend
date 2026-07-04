-- 0043_goal_estimates.sql
--
-- Adds a configured, per-tier time estimate to the goals system so the Goals
-- page and timeline can show realistic, scale-aware target dates.
--
-- Product rule (a goal's clock starts only when it becomes active):
--   * estimate_days is a *configured duration*, not a deadline. It does not start
--     counting until the tier becomes the active milestone for a given owner.
--   * When a tier becomes active, activated_at = the previous tier's completion
--     date (or the group's start anchor for tier 0) and target_date =
--     activated_at + estimate_days. Both are DERIVED at read time from the
--     append-only achievement log (services/goals.py); nothing is stored here
--     except the configured estimate itself.
--
-- Purely additive and idempotent (IF NOT EXISTS / value updates are safe to
-- re-apply). No data migration required.

-- 1. Configured estimate (in days) for how long a tier is expected to take once
--    it becomes active. NULL falls back to a computed, scale-aware estimate.
ALTER TABLE goal_tiers ADD COLUMN IF NOT EXISTS estimate_days INTEGER;

-- 2. Seed a realistic, increasing schedule per group: consecutive small tiers are
--    close together; large jumps (e.g. 10k -> 100k) are months apart, so the
--    timeline reflects progression and scale rather than even spacing.
UPDATE goal_tiers t SET estimate_days = v.days
FROM (VALUES
    -- Signups: 5, 10, 50, 100, 500, 1000
    ('proj_signups', 0, 21), ('proj_signups', 1, 30), ('proj_signups', 2, 60),
    ('proj_signups', 3, 90), ('proj_signups', 4, 150), ('proj_signups', 5, 240),
    -- Revenue (MRR): First $, $100, $1k, $10k
    ('proj_revenue', 0, 14), ('proj_revenue', 1, 45), ('proj_revenue', 2, 120), ('proj_revenue', 3, 240),
    -- Traffic (pageviews): 100, 1k, 10k, 100k
    ('proj_traffic', 0, 21), ('proj_traffic', 1, 45), ('proj_traffic', 2, 120), ('proj_traffic', 3, 240),
    -- Product setup (of 4 connections): quick, days apart
    ('proj_setup', 0, 3), ('proj_setup', 1, 3), ('proj_setup', 2, 4), ('proj_setup', 3, 4),
    -- Issue tracking: 1, 5, 20
    ('proj_issues', 0, 7), ('proj_issues', 1, 21), ('proj_issues', 2, 45),
    -- Launch products: 1, 5, 10, 25
    ('acct_launches', 0, 30), ('acct_launches', 1, 180), ('acct_launches', 2, 365), ('acct_launches', 3, 730),
    -- Problem discovery: 1, 20, 100, 500
    ('acct_problems', 0, 3), ('acct_problems', 1, 30), ('acct_problems', 2, 90), ('acct_problems', 3, 180),
    -- Task management: 1, 20, 100
    ('acct_tasks', 0, 2), ('acct_tasks', 1, 21), ('acct_tasks', 2, 60),
    -- Team: 1, 5
    ('acct_team', 0, 7), ('acct_team', 1, 30)
) AS v(goal_id, tier_index, days)
WHERE t.goal_definition_id = v.goal_id AND t.tier_index = v.tier_index;
