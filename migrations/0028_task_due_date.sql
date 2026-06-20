-- 0028_task_due_date.sql
--
-- Phase 2 of the launch-timeline feature: per-task due dates. A task can carry
-- an optional calendar due date, anchored (in the UI) to the project's launch
-- window. Day-granular, so a plain DATE rather than a timestamptz.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS due_date DATE;
