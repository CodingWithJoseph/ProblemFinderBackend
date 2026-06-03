-- add_cluster_is_singleton.sql
--
-- BACKEND JOB 4 — flag single-post clusters so the public endpoints can exclude
-- them by default. Mirrors Cluster.is_singleton in app/models.py.

ALTER TABLE clusters
  ADD COLUMN IF NOT EXISTS
  is_singleton BOOLEAN DEFAULT FALSE;
