-- 0009: Promote opportunity_type and drop the legacy type column.
-- The opportunity_type column already exists on the opportunities table
-- (written by the AI pipeline). This migration back-fills any rows where
-- opportunity_type is NULL but the old type column has a value, then drops
-- the now-redundant type column.

UPDATE opportunities
    SET opportunity_type = type
    WHERE opportunity_type IS NULL AND type IS NOT NULL;

ALTER TABLE opportunities
    DROP COLUMN IF EXISTS type;
