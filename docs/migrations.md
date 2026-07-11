# Database migrations

Schema changes live in `migrations/` as numbered plain-SQL files
(`NNNN_description.sql`) and are applied **in numeric order**. Migrations should
be additive and idempotent where possible (`IF NOT EXISTS`, `ON CONFLICT DO
NOTHING`).

## Applying migrations

The runner records which migrations have run in a `schema_migrations` table and
applies only the pending ones, each in its own transaction:

```bash
python scripts/migrate.py            # apply all pending migrations
python scripts/migrate.py --status   # list applied / pending, apply nothing
python scripts/migrate.py --dry-run  # print what would run, apply nothing
```

The database URL comes from `settings.database_url` (the same `DATABASE_URL` the
app uses).

## Adopting the runner on an existing database

If your database already had migrations applied by hand (before this runner
existed), baseline it once so those aren't re-run:

```bash
python scripts/migrate.py --baseline 0045
```

This records every migration up to and including `0045` as applied **without
executing it**. Afterwards, `python scripts/migrate.py` applies only newer files.

## Adding a migration

1. Create `migrations/NNNN_short_description.sql` with the next number.
2. Prefer additive, idempotent DDL.
3. Run `python scripts/migrate.py` (or `--dry-run` first) to apply it.
