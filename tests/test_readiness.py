from app.readiness import check_database_readiness


class FakeScalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return FakeScalars(self.values)


class FakeConnection:
    def __init__(self, versions=None, error=None):
        self.versions = versions or []
        self.error = error
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        if self.error:
            raise self.error
        return FakeResult(self.versions if self.calls == 2 else [1])


class ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return ConnectionContext(self.connection)


async def test_readiness_passes_when_release_migrations_are_applied():
    status = await check_database_readiness(
        FakeEngine(FakeConnection([
            "0050_solution_warroom",
            "0051_ide_mcp_access",
            "0052_build_contract_foundation",
            "0053_mcp_oauth",
            "0055_supabase_accounts",
        ]))
    )

    assert status.ready is True
    assert status.missing_migrations == ()


async def test_readiness_lists_missing_release_migrations():
    status = await check_database_readiness(
        FakeEngine(FakeConnection(["0050_solution_warroom"]))
    )

    assert status.ready is False
    assert status.missing_migrations == (
        "0051_ide_mcp_access",
        "0052_build_contract_foundation",
        "0053_mcp_oauth",
            "0055_supabase_accounts",
    )


async def test_readiness_hides_database_errors():
    status = await check_database_readiness(
        FakeEngine(FakeConnection(error=RuntimeError("secret database detail")))
    )

    assert status.ready is False
    assert status.missing_migrations == ()
