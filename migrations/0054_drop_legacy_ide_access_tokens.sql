-- 0054_drop_legacy_ide_access_tokens.sql
-- Manual project-scoped MCP tokens were replaced by account OAuth.

DROP TABLE IF EXISTS public.ide_access_tokens;
