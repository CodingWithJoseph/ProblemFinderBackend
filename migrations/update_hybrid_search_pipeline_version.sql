-- update_hybrid_search_pipeline_version.sql
--
-- BACKEND JOB 1 — pipeline_version filter for hybrid_search().
--
-- The hybrid_search() function is defined directly in Supabase (it is only
-- *called* from app/routes/opportunities.py:search_opportunities — it is not
-- defined anywhere in this repository). To add the pipeline_version = 'v2'
-- filter "at the source" (so /opportunities/search only ever returns v2 rows),
-- this migration redefines the function with that predicate baked in.
--
-- The literal 'v2' below intentionally mirrors app.config.PIPELINE_VERSION.
-- SQL cannot import the Python constant; this migration is the single place the
-- value is repeated, and it must be kept in sync with PIPELINE_VERSION.
--
-- hybrid_search RETURNS SETOF opportunities — every selected column must remain
-- a real opportunities column so the route can rebuild Opportunity(**row).
--
-- NOTE: This reconstructs the canonical weighted full-text + vector hybrid
-- search. If the live Supabase definition differs (e.g. different weights or an
-- RRF blend), preserve that scoring and apply ONLY the two added predicates:
--     o.deleted_at IS NULL
--     o.pipeline_version = 'v2'

CREATE OR REPLACE FUNCTION hybrid_search(
    query_text   text,
    query_embedding vector(1536),
    match_count  int DEFAULT 200,
    full_text_weight float DEFAULT 1.0,
    semantic_weight  float DEFAULT 1.0,
    rrf_k        int DEFAULT 50
)
RETURNS SETOF opportunities
LANGUAGE sql
STABLE
AS $$
WITH full_text AS (
    SELECT
        o.id,
        row_number() OVER (
            ORDER BY ts_rank_cd(o.search_vector, websearch_to_tsquery(query_text)) DESC
        ) AS rank_ix
    FROM opportunities o
    WHERE o.deleted_at IS NULL
      AND o.pipeline_version = 'v2'
      AND o.search_vector @@ websearch_to_tsquery(query_text)
    ORDER BY rank_ix
    LIMIT LEAST(match_count, 500) * 2
),
semantic AS (
    SELECT
        o.id,
        row_number() OVER (
            ORDER BY o.embedding <=> query_embedding
        ) AS rank_ix
    FROM opportunities o
    WHERE o.deleted_at IS NULL
      AND o.pipeline_version = 'v2'
      AND o.embedding IS NOT NULL
    ORDER BY rank_ix
    LIMIT LEAST(match_count, 500) * 2
)
SELECT o.*
FROM full_text
FULL OUTER JOIN semantic ON full_text.id = semantic.id
JOIN opportunities o
    ON o.id = COALESCE(full_text.id, semantic.id)
WHERE o.deleted_at IS NULL
  AND o.pipeline_version = 'v2'
ORDER BY
    COALESCE(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight
  + COALESCE(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight
    DESC
LIMIT LEAST(match_count, 500);
$$;
