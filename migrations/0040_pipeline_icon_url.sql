-- Optional product icon used by Pipeline and Portfolio project identity UI.
ALTER TABLE pipeline
ADD COLUMN IF NOT EXISTS icon_url TEXT;
