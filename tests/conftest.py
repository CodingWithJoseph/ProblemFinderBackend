"""Shared fixtures and test doubles for the ProblemFinder backend API suite.

Strategy
--------
* The real app reads required settings from the environment and performs
  import-time side effects (Firebase init, async engine creation, OpenAI client
  construction). We set dummy settings and stub ``firebase_admin`` *before*
  importing any ``app.*`` module.
* We never touch a real database. Instead of the production Postgres-specific
  models (pgvector / JSONB / TSVECTOR can't run on SQLite), we override the
  ``get_db`` dependency with a :class:`FakeSession` that returns a pre-seeded,
  ordered queue of results — one entry per ``execute``/``scalar`` call the route
  makes. This keeps tests fast, hermetic, and free of a live DB.
* ``get_uid`` is overridden so requests are authenticated as ``test-uid``
  without verifying a real Firebase token.
"""

import os
import sys
import uuid
from datetime import datetime, date, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Ensure the repository root is importable (so ``import app`` works) regardless
# of how pytest is invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ──────────────────────────────────────────────────────────────────────────
# Environment + heavy-import stubs (must run before importing app.*)
# ──────────────────────────────────────────────────────────────────────────

# Postgres URL so the production async engine (which passes pool_size /
# max_overflow) is created without error. The engine is lazy and never connects
# because the get_db dependency is overridden in tests.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_CREDENTIALS_PATH", "unused-in-tests.json")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("STRIPE_PRO_MONTHLY_PRICE_ID", "price_pro_m")
os.environ.setdefault("STRIPE_PRO_YEARLY_PRICE_ID", "price_pro_y")
os.environ.setdefault("STRIPE_ELITE_MONTHLY_PRICE_ID", "price_elite_m")
os.environ.setdefault("STRIPE_ELITE_YEARLY_PRICE_ID", "price_elite_y")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")

# firebase-admin is not installed in the test environment and would otherwise
# try to load real service-account credentials at import time.
sys.modules.setdefault("firebase_admin", MagicMock())

from fastapi import FastAPI  # noqa: E402

from app.db import get_db  # noqa: E402
from app.auth import get_uid  # noqa: E402
from app.models import (  # noqa: E402
    Opportunity,
    Subscription,
    PlanEnum,
    Cluster,
    ClusterSnapshot,
    ClusterMember,
    Pipeline,
    Problem,
    SourcePost,
)
from app.routes.opportunities import router as opportunities_router  # noqa: E402
from app.routes.clusters import router as clusters_router  # noqa: E402
from app.routes.problems import router as problems_router  # noqa: E402
from app.routes.pipeline import router as pipeline_router  # noqa: E402
from app.routes.dashboard import router as dashboard_router  # noqa: E402
from app.routes.homepage import router as homepage_router  # noqa: E402

TEST_UID = "test-uid"


# ──────────────────────────────────────────────────────────────────────────
# Fake async SQLAlchemy session
# ──────────────────────────────────────────────────────────────────────────

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
    """Stands in for a SQLAlchemy ``Result``.

    Supply ``rows`` for ORM/row results, ``mappings`` for ``text()`` queries that
    use ``.mappings()``, or ``scalar`` to force a specific scalar return.
    """

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
    """Async-session test double driven by ordered result queues.

    Each ``await session.execute(...)`` pops the next entry from
    ``execute_results``; each ``await session.scalar(...)`` pops from
    ``scalar_results``. Tests enumerate the exact calls a route makes.
    """

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
            return self.execute_results.pop(0)
        return FakeResult()

    async def scalar(self, statement=None, params=None):
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None

    def add(self, obj):
        self.added.append(obj)
        # Simulate the DB assigning a primary key on flush so freshly-created
        # rows serialize with a real id.
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


# ──────────────────────────────────────────────────────────────────────────
# Row builders (importable helpers used by the data fixtures below)
# ──────────────────────────────────────────────────────────────────────────

def make_subscription(**kw):
    defaults = dict(uid=TEST_UID, plan=PlanEnum.free)
    defaults.update(kw)
    return Subscription(**defaults)


def make_opportunity(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        source_id="reddit-1",
        title="Invoicing eats my whole week",
        description="Freelancers waste hours chasing invoices.",
        source="reddit",
        source_url="https://reddit.com/r/freelance/1",
        opportunity_type="software",
        source_score=120.0,
        trending=True,
        cluster_id=None,
        num_comments=12,
        upvote_ratio=0.95,
        feed="hot",
        author="freelancer42",
        scraped_at=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        deleted_at=None,
        pipeline_count=0,
        launch_count=0,
    )
    defaults.update(kw)
    return Opportunity(**defaults)


def opportunity_row(**kw):
    """A mapping-style row dict as returned by raw ``text()`` SQL (hybrid search,
    similar-posts). The route reconstruction whitelists mapped columns, so any
    stale scoring columns in the row are ignored."""
    row = dict(
        id=str(uuid.uuid4()),
        source_id="reddit-2",
        title="Manual reconciliation is painful",
        description="I copy numbers between three tools.",
        source="reddit",
        source_url="https://reddit.com/r/smallbusiness/2",
        opportunity_type="software",
        source_score=80.0,
        trending=False,
        cluster_id=None,
        num_comments=8,
        upvote_ratio=0.9,
        feed="hot",
        author="ownerjane",
        scraped_at=None,
        created_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        pipeline_count=0,
        launch_count=0,
    )
    row.update(kw)
    return row


def make_cluster(**kw):
    defaults = dict(
        id=42,
        name="Freelance invoicing pain",
        first_seen=date(2026, 5, 1),
        source="reddit",
        trending=True,
        growth_rate_wow=0.35,
        growth_rate_mom=1.2,
        post_velocity=140.0,
    )
    defaults.update(kw)
    return Cluster(**defaults)


def make_snapshot(**kw):
    defaults = dict(
        id=1,
        cluster_id=42,
        date=date(2026, 5, 20),
        post_count=50,
        sample_posts=[],
    )
    defaults.update(kw)
    return ClusterSnapshot(**defaults)


def make_source_post(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        source="reddit",
        source_post_id="reddit-1",
        source_community_id="r/freelance",
        source_author_id="freelancer42",
        title="Invoicing eats my whole week",
        body="Freelancers waste hours chasing invoices.",
        url="https://reddit.com/r/freelance/1",
        top_comments=[{"body": "Same problem here"}],
        posted_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        scraped_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        score=120,
        upvote_ratio=0.95,
        num_comments=12,
        is_deleted=False,
        is_removed=False,
    )
    defaults.update(kw)
    return SourcePost(**defaults)


def make_cluster_member(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        cluster_id=42,
        source_post_id=str(uuid.uuid4()),
        similarity_score=0.91,
        membership_confidence=0.88,
        assigned_by="model",
        model_version="v2",
    )
    defaults.update(kw)
    return ClusterMember(**defaults)


def make_pipeline(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        user_id=TEST_UID,
        name="Freelance invoicing",
        post_ids=[],
        source_cluster_id=None,
        stage="watching",
        kill_criteria=None,
        distribution_channels=[],
        concept_angles=[],
        exit_checklist=None,
        notes="early notes",
        url=None,
        category=None,
        revenue_model=None,
        status="active",
        cluster_metrics=None,
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
    """Mimic the OpenAI embeddings.create(...) response shape."""
    return SimpleNamespace(data=[SimpleNamespace(embedding=list(vector))])


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_db():
    return FakeSession()


@pytest.fixture
def app(fake_db):
    application = FastAPI()
    application.include_router(opportunities_router)
    application.include_router(clusters_router)
    application.include_router(problems_router)
    application.include_router(pipeline_router)
    application.include_router(dashboard_router)
    application.include_router(homepage_router)

    async def _get_db_override():
        yield fake_db

    application.dependency_overrides[get_db] = _get_db_override
    application.dependency_overrides[get_uid] = lambda: TEST_UID

    from app.limits import register_limit_handler
    register_limit_handler(application)
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    # get_uid is overridden, so the token value is irrelevant; included so
    # requests look like the real authenticated calls.
    return {"Authorization": "Bearer test-token"}


# Convenience data fixtures (factories so tests can override fields).
@pytest.fixture
def opportunity_factory():
    return make_opportunity


@pytest.fixture
def cluster_factory():
    return make_cluster


@pytest.fixture
def problem_factory():
    return make_problem


@pytest.fixture
def pipeline_factory():
    return make_pipeline
