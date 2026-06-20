-- 0027_pipeline_timeline.sql
--
-- Phase 1 launch-timeline support for the project creation flow. A project
-- (pipeline card) can carry a user-given name distinct from the source cluster
-- name, plus an optional fixed-length launch timeline.
--
--   * ``project_name``           — user-given name; falls back to ``name``
--                                  (the cluster name) when null.
--   * ``timeline_days``          — chosen window: 14, 30, 60 or 90 days.
--   * ``timeline_start``         — set to "now" when the timeline is chosen;
--                                  never asked of the user.
--   * ``timeline_target_launch`` — timeline_start + timeline_days, stored on
--                                  save so the target is stable.

ALTER TABLE pipeline
    ADD COLUMN IF NOT EXISTS project_name TEXT,
    ADD COLUMN IF NOT EXISTS timeline_days INTEGER,
    ADD COLUMN IF NOT EXISTS timeline_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS timeline_target_launch TIMESTAMPTZ;
