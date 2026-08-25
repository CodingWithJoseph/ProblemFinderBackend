
"""Shared fixtures and test doubles for the ProblemFinder backend API suite.

Strategy
--------
* We set dummy settings and stub ``firebase_admin`` *before* importing any
  ``app.*`` module (the real app reads required settings and performs
  import-time side effects).
* We never touch a real database. ``get_db`` is overridden with a
  :class:`FakeSession` that returns a pre-seeded, ordered queue of results â€” one
  entry per ``execute``/``scalar`` call the route makes.
* ``get_uid`` is overridden so requests authenticate as ``test-uid``.
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Environment + heavy-import stubs (must run before importing app.*)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_CREDENTIALS_PATH", "unused-in-tests.json")
os.environ.setdefault("AI_PROVIDER", "ollama")
os.environ.setdefault("AI_BASE_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("AI_MODEL", "gpt-oss:20b")
os.environ.setdefault("AI_API_KEY", "")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("SIGNAL_ANALYSIS_WORKER_ENABLED", "false")
os.environ.setdefault("SOLUTION_WORKER_ENABLED", "false")

sys.modules.setdefault("firebase_admin", MagicMock())

from fastapi import FastAPI  # noqa: E402

from app.db import get_db  # noqa: E402
from app.auth import get_uid  # noqa: E402
from app.models import (  # noqa: E402
    Cluster,
    ClusterItem,
    SearchRun,
    SearchSession,
    SearchTurn,
    Pipeline,
    Problem,
)
from app.routes.clusters import router as clusters_router  # noqa: E402
from app.routes.search_sessions import router as search_sessions_router  # noqa: E402
from app.routes.signal_workspace import router as signal_workspace_router  # noqa: E402
from app.routes.solution_workspace import router as solution_workspace_router  # noqa: E402
from app.routes.ide_workspace import router as ide_workspace_router  # noqa: E402
from app.routes.problems import router as problems_router  # noqa: E402
from app.routes.pipeline import router as pipeline_router  # noqa: E402

TEST_UID = "test-uid"


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Fake async SQLAlchemy session
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_UNSET = object()


class FakeScalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeMappings:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def one(self):
        return self._rows[0]

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self._rows[0] if self._rows else None


class FakeResult:
    def __init__(self, rows=None, mappings=None, scalar=_UNSET):
        self._rows = list(rows) if rows is not None else []
        self._mappings = list(mappings) if mappings is not None else []
        self._scalar = scalar

    def scalars(self):
        return FakeScalars(self._rows)

    def mappings(self):
        return FakeMappings(self._mappings)

    def scalar_one_or_none(self):
        if self._scalar is not _UNSET:
            return self._scalar
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        if self._scalar is not _UNSET:
            return self._scalar
        return self._rows[0]

    def scalar(self):
        if self._scalar is not _UNSET:
            return self._scalar
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    def __init__(self):
        self.execute_results = []
        self.scalar_results = []
        self.added = []
        self.deleted = []
        self.commit_count = 0
        self.flush_count = 0

    def stub(self, execute=None, scalar=None):
        if execute is not None:
            self.execute_results = list(execute)
        if scalar is not None:
            self.scalar_results = list(scalar)
        return self

    async def execute(self, statement, params=None):
        if self.execute_results:
            result = self.execute_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return FakeResult()

    async def scalar(self, statement=None, params=None):
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            try:
                obj.id = str(uuid.uuid4())
            except Exception:
                pass

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        pass

    async def flush(self):
        self.flush_count += 1

    async def refresh(self, obj, *args, **kwargs):
        pass

    async def close(self):
        pass


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Row builders
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def make_cluster(**kw):
    defaults = dict(
        id=42,
        name="Freelance invoicing pain",
        summary="Freelancers struggle to get invoices paid on time.",
        pipeline_version="v2",
    )
    defaults.update(kw)
    return Cluster(**defaults)


def make_cluster_item(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        cluster_id=42,
        pipeline_version="v2",
        platform="reddit",
        community="r/freelance",
        source_item_id="reddit-1",
        title="Invoicing eats my whole week",
        body="Freelancers waste hours chasing invoices.",
        url="https://reddit.com/r/freelance/1",
        author="freelancer42",
        score=120,
        num_comments=12,
        posted_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        raw_json={"upvote_ratio": 0.95, "top_comments": [{"body": "Same problem here"}]},
        problem_statement="Freelancers waste hours every week chasing unpaid invoices.",
        similarity_score=0.91,
    )
    defaults.update(kw)
    return ClusterItem(**defaults)


def make_pipeline(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        user_id=TEST_UID,
        name="Freelance invoicing",
        post_ids=[],
        source_cluster_id=None,
        stage="watching",
        launched_at=None,
        removed_at=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Pipeline(**defaults)


def make_problem(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        user_id=TEST_UID,
        title="Cannot reconcile invoices across tools",
        description="Numbers never match.",
        source_post_id=None,
        position=0,
        embedding=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Problem(**defaults)


def embedding_response(vector):
    return SimpleNamespace(data=[SimpleNamespace(embedding=list(vector))])


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Fixtures
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.fixture
def fake_db():
    return FakeSession()


@pytest.fixture
def app(fake_db):
    application = FastAPI()
    application.include_router(clusters_router)
    application.include_router(search_sessions_router)
    application.include_router(signal_workspace_router)
    application.include_router(solution_workspace_router)
    application.include_router(ide_workspace_router)
    application.include_router(problems_router)
    application.include_router(pipeline_router)

    async def _get_db_override():
        yield fake_db

    application.dependency_overrides[get_db] = _get_db_override
    application.dependency_overrides[get_uid] = lambda: TEST_UID

    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def cluster_factory():
    return make_cluster


@pytest.fixture
def cluster_item_factory():
    return make_cluster_item


@pytest.fixture
def problem_factory():
    return make_problem


@pytest.fixture
def pipeline_factory():
    return make_pipeline
