"""Apply pending SQL migrations, tracked in a ``schema_migrations`` table.

Until now migrations under ``migrations/*.sql`` were applied by hand in order.
This runner records which have run (by filename) in a ``schema_migrations`` table
and applies only the pending ones, in numeric order, each in its own transaction.

Usage (from the repo root)::

    python scripts/migrate.py                # apply all pending migrations
    python scripts/migrate.py --status       # show applied / pending, apply nothing
    python scripts/migrate.py --dry-run      # print what would run, apply nothing
    python scripts/migrate.py --baseline 0045
                                             # mark everything up to and including
                                             # 0045 as applied WITHOUT running it —
                                             # use once when adopting the runner on a
                                             # database whose migrations were already
                                             # applied by hand.

The DB URL comes from ``settings.database_url`` (the same one the app uses); the
``+asyncpg`` dialect suffix is stripped for a direct asyncpg connection.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

MIGRATIONS_DIR = _REPO_ROOT / "migrations"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class Migration:
    version: str  # filename without the .sql suffix, e.g. "0045_project_journey_goals"
    path: Path

    @property
    def number(self) -> str:
        """Leading numeric id, e.g. "0045" — used for --baseline comparisons."""
        return self.version.split("_", 1)[0]


# ── Pure helpers (unit-tested; no DB) ────────────────────────────────────────

def discover_migrations(directory: Path) -> list[Migration]:
    """All ``NNNN_*.sql`` migrations in the directory, sorted by filename."""
    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    return [Migration(version=p.stem, path=p) for p in files]


def pending_migrations(all_migrations: list[Migration], applied: set[str]) -> list[Migration]:
    """Migrations not yet recorded as applied, in order."""
    return [m for m in all_migrations if m.version not in applied]


def migrations_up_to(all_migrations: list[Migration], number: str) -> list[Migration]:
    """Every migration whose numeric id is <= ``number`` (for --baseline)."""
    return [m for m in all_migrations if m.number <= number]


# ── DB access ────────────────────────────────────────────────────────────────

def _asyncpg_dsn() -> str:
    from app.config import get_settings

    url = get_settings().database_url
    # asyncpg connects with a plain postgres URL; drop SQLAlchemy's dialect suffix.
    return url.replace("+asyncpg", "").replace("postgresql+psycopg", "postgresql")


async def _connect():
    import asyncpg

    return await asyncpg.connect(_asyncpg_dsn())


async def _applied_versions(conn) -> set[str]:
    await conn.execute(CREATE_TABLE_SQL)
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def _record(conn, version: str) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (version) VALUES ($1) "
        "ON CONFLICT (version) DO NOTHING",
        version,
    )


async def run(*, status_only: bool, dry_run: bool, baseline: str | None) -> int:
    all_migrations = discover_migrations(MIGRATIONS_DIR)
    conn = await _connect()
    try:
        applied = await _applied_versions(conn)

        if baseline is not None:
            marked = 0
            for m in migrations_up_to(all_migrations, baseline):
                if m.version not in applied:
                    await _record(conn, m.version)
                    marked += 1
            print(f"Baselined {marked} migration(s) up to {baseline} (not executed).")
            return 0

        pending = pending_migrations(all_migrations, applied)

        if status_only or dry_run:
            print(f"{len(applied)} applied, {len(pending)} pending.")
            for m in pending:
                print(f"  pending: {m.version}")
            return 0

        if not pending:
            print("No pending migrations.")
            return 0

        for m in pending:
            sql = m.path.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await _record(conn, m.version)
            print(f"Applied {m.version}")
        print(f"Done: applied {len(pending)} migration(s).")
        return 0
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending SQL migrations.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show applied/pending, apply nothing")
    group.add_argument("--dry-run", action="store_true", help="print pending migrations, apply nothing")
    group.add_argument("--baseline", metavar="NUMBER", help="mark migrations up to NUMBER as applied without running them")
    args = parser.parse_args(argv)
    return asyncio.run(run(status_only=args.status, dry_run=args.dry_run, baseline=args.baseline))


if __name__ == "__main__":
    raise SystemExit(main())
