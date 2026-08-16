"""Production readiness checks for the API and its release schema."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


REQUIRED_SCHEMA_VERSIONS = frozenset(
    {
        "0050_solution_warroom",
        "0051_ide_mcp_access",
        "0052_build_contract_foundation",
    }
)


@dataclass(frozen=True)
class ReadinessStatus:
    ready: bool
    missing_migrations: tuple[str, ...] = ()


async def check_database_readiness(db_engine: AsyncEngine) -> ReadinessStatus:
    """Return a safe readiness result without exposing database errors."""

    try:
        async with db_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            result = await connection.execute(text("SELECT version FROM schema_migrations"))
            applied = set(result.scalars().all())
    except Exception:
        return ReadinessStatus(ready=False)

    missing = tuple(sorted(REQUIRED_SCHEMA_VERSIONS - applied))
    return ReadinessStatus(ready=not missing, missing_migrations=missing)
