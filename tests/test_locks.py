from app.services import locks


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    def __init__(self, acquired):
        self.acquired = acquired
        self.statements = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self.acquired)
        return _FakeResult(None)

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, acquired):
        self.conn = _FakeConn(acquired)

    def connect(self):
        return self.conn


async def test_runs_job_when_lock_acquired(monkeypatch):
    engine = _FakeEngine(acquired=True)
    monkeypatch.setattr(locks, "engine", engine)
    calls = []

    async def job():
        calls.append(1)
        return "done"

    result = await locks.run_with_advisory_lock(123, job, name="t")

    assert result == "done"
    assert calls == [1]
    # Acquired then released.
    assert any("pg_try_advisory_lock" in s for s in engine.conn.statements)
    assert any("pg_advisory_unlock" in s for s in engine.conn.statements)


async def test_skips_job_when_lock_busy(monkeypatch):
    engine = _FakeEngine(acquired=False)
    monkeypatch.setattr(locks, "engine", engine)
    calls = []

    async def job():
        calls.append(1)
        return "done"

    result = await locks.run_with_advisory_lock(123, job, name="t")

    assert result is None
    assert calls == []  # job never ran
    # No unlock when we never acquired.
    assert not any("pg_advisory_unlock" in s for s in engine.conn.statements)
