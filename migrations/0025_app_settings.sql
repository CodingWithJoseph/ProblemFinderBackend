-- 0025_app_settings.sql
--
-- Admin-editable global configuration overrides. Each row overrides one knob
-- whose default lives in app/config.py Settings; absence means "use the config
-- default". Only the whitelisted keys in app/services/app_settings.py are ever
-- read or written, and values are validated against that whitelist before save.

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
