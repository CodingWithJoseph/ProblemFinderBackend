ALTER TABLE public.clusters
ADD COLUMN IF NOT EXISTS discovery_category TEXT
    NOT NULL
    DEFAULT 'uncategorized';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.clusters'::regclass
          AND conname = 'clusters_discovery_category_valid'
    ) THEN
        ALTER TABLE public.clusters
        ADD CONSTRAINT clusters_discovery_category_valid
        CHECK (
            discovery_category IN (
                'build_and_tooling',
                'data_and_apis',
                'workflow_and_automation',
                'uncategorized'
            )
        );
    END IF;
END;
$$;