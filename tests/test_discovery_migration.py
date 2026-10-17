"""Verify the category migration on new and manually updated local schemas."""

import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest


DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="Set TEST_DATABASE_URL to isolated local PostgreSQL")


@pytest.mark.parametrize("column_already_exists", [False, True])
async def test_category_migration_is_repeatable_and_enforces_values(column_already_exists):
    dsn = DSN.replace("postgresql+asyncpg", "postgresql")
    url = urlparse(dsn)
    assert url.hostname in {"127.0.0.1", "localhost"} and url.path == "/auth_migration", (
        "Refusing a non-test database"
    )
    schema = f"discovery_migration_{uuid.uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    created = False
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        column = ", discovery_category TEXT NOT NULL DEFAULT 'uncategorized'" if column_already_exists else ""
        await connection.execute(f'CREATE TABLE "{schema}".clusters (id INTEGER PRIMARY KEY{column})')
        await connection.execute(f'INSERT INTO "{schema}".clusters (id) VALUES (1)')
        migration = (Path(__file__).resolve().parents[1] / "migrations/0056_discovery_category.sql").read_text()
        # Scope both DDL and the regclass lookup to this test's own schema.
        migration = migration.replace("public.clusters", f"{schema}.clusters")
        await connection.execute(migration)
        await connection.execute(migration)
        assert await connection.fetchval(f'SELECT discovery_category FROM "{schema}".clusters WHERE id=1') == "uncategorized"
        assert await connection.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='clusters' AND column_name='discovery_category'",
            schema,
        ) == "NO"
        await connection.execute(f'INSERT INTO "{schema}".clusters (id) VALUES (2)')
        assert await connection.fetchval(f'SELECT discovery_category FROM "{schema}".clusters WHERE id=2') == "uncategorized"
        for index, category in enumerate(("build_and_tooling", "data_and_apis", "workflow_and_automation"), 3):
            await connection.execute(
                f'INSERT INTO "{schema}".clusters (id, discovery_category) VALUES ($1,$2)', index, category,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(f"INSERT INTO \"{schema}\".clusters VALUES (99, 'invalid')")
        with pytest.raises(asyncpg.NotNullViolationError):
            await connection.execute(f'INSERT INTO "{schema}".clusters VALUES (100, NULL)')
    finally:
        if created:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()
