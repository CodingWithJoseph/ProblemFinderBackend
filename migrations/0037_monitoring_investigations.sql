-- 0037_monitoring_investigations.sql
--
-- Investigate & Report (Phase 4). An investigation is a case object that
-- collects evidence and notes into a timeline; a report is a saved, exportable
-- write-up. Monitoring domain — distinct from discovery `problems`/issues.

CREATE TABLE IF NOT EXISTS monitoring_investigations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT monitoring_investigations_status_check CHECK (status IN ('open', 'resolved', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_monitoring_investigations_pipeline
    ON monitoring_investigations(pipeline_id, created_at);

-- Evidence + notes share one table: ordered by created_at they form the
-- investigation timeline. `kind` discriminates a free note from a linked object.
CREATE TABLE IF NOT EXISTS monitoring_investigation_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    investigation_id UUID NOT NULL REFERENCES monitoring_investigations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ref_id TEXT,
    body TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT monitoring_entries_kind_check CHECK (kind IN ('note', 'issue', 'problem', 'session', 'link'))
);

CREATE INDEX IF NOT EXISTS idx_monitoring_entries_investigation
    ON monitoring_investigation_entries(investigation_id, created_at);

CREATE TABLE IF NOT EXISTS monitoring_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    investigation_id UUID REFERENCES monitoring_investigations(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_monitoring_reports_pipeline
    ON monitoring_reports(pipeline_id, created_at);
