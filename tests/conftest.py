"""Shared fixtures and test doubles for the ProblemFinder backend API suite.

Strategy
--------
* We set dummy settings and stub ``firebase_admin`` *before* importing any
  ``app.*`` module (the real app reads required settings and performs
  import-time side effects).
* We never touch a real database. ``get_db`` is overridden with a
  :class:`FakeSession` that returns a pre-seeded, ordered queue of results — one
  entry per ``execute``/``scalar`` call the route makes.
* ``get_uid`` is overridden so requests authenticate as ``test-uid``.
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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ──────────────────────────────────────────────────────────────────────────
# Environment + heavy-import stubs (must run before importing app.*)
# ──────────────────────────────────────────────────────────────────────────

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
os.environ.setdefault("SIGNAL_ANALYSIS_WORKER_ENABLED", "false")

sys.modules.setdefault("firebase_admin", MagicMock())
stripe_stub = MagicMock()
stripe_stub.OAuth.token = MagicMock(return_value={"stripe_user_id": "acct_test"})
sys.modules.setdefault("stripe", stripe_stub)

from fastapi import FastAPI  # noqa: E402

from app.db import get_db  # noqa: E402
from app.auth import get_uid  # noqa: E402
from app.models import (  # noqa: E402
    Subscription,
    PlanEnum,
    Cluster,
    ClusterSignal,
    ClusterItem,
    ClusterSnapshot,
    SearchRun,
    SearchSession,
    SearchTurn,
    Pipeline,
    Problem,
    Task,
    Team,
    TeamMember,
    Issue,
    IssueComment,
    MonitorUsageSource,
    MonitorUsageEvent,
    MonitorErrorGroup,
    MonitorErrorEvent,
    MonitorRevenueSource,
    UserPreference,
)
from app.routes.clusters import router as clusters_router  # noqa: E402
from app.routes.search_sessions import router as search_sessions_router  # noqa: E402
from app.routes.signal_workspace import router as signal_workspace_router  # noqa: E402
from app.routes.problems import router as problems_router  # noqa: E402
from app.routes.pipeline import router as pipeline_router  # noqa: E402
from app.routes.monitor_ingest import public_router as monitor_public_router  # noqa: E402
from app.routes.monitor import router as monitor_router  # noqa: E402
from app.routes.monitor_war_room import router as monitor_war_room_router  # noqa: E402
from app.routes.portfolio import router as portfolio_router  # noqa: E402
from app.routes.goals import router as goals_router  # noqa: E402
from app.routes.tasks import router as tasks_router  # noqa: E402
from app.routes.teams import router as teams_router  # noqa: E402
from app.routes.teams import invites_router as invites_router  # noqa: E402
from app.routes.issues import router as issues_router  # noqa: E402
from app.routes.dashboard import router as dashboard_router  # noqa: E402
from app.routes.homepage import router as homepage_router  # noqa: E402
from app.routes.public import router as public_router  # noqa: E402
from app.routes.preferences import router as preferences_router  # noqa: E402

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


# ──────────────────────────────────────────────────────────────────────────
# Row builders
# ──────────────────────────────────────────────────────────────────────────

def make_subscription(**kw):
    defaults = dict(uid=TEST_UID, plan=PlanEnum.free)
    defaults.update(kw)
    return Subscription(**defaults)


def make_user_preference(**kw):
    defaults = dict(
        uid=TEST_UID,
        alerts_email_enabled=True,
        digest_cadence="instant",
        alert_email=None,
        default_pipeline_id=None,
        default_landing=None,
        last_digest_sent_at=None,
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return UserPreference(**defaults)


def make_cluster(**kw):
    defaults = dict(
        id=42,
        name="Freelance invoicing pain",
        summary="Freelancers struggle to get invoices paid on time.",
        signal_score=0.82,
        pipeline_version="v2",
        trending=True,
        first_seen=date(2026, 5, 1),
        last_seen_date=date(2026, 5, 20),
        author_count=37,
        community_count=4,
        source_breakdown=[{"site": "r/freelance", "count": 12}],
        post_volume_by_date=[{"date": "2026-05-01", "count": 3}],
    )
    defaults.update(kw)
    return Cluster(**defaults)


def make_cluster_signal(**kw):
    defaults = dict(
        cluster_id=42,
        input_fingerprint="fp-42",
        generated_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        status="ready",
        last_error=None,
        signal_score=0.91,
        recency=0.88,
        momentum_7d=0.2,
        momentum_30d=0.4,
        momentum_90d=None,
        post_volume_by_week=[{"week": "2026-W20", "count": 4}, {"week": "2026-W21", "count": 7}],
        total_posts=12,
        author_count=8,
        community_count=3,
        platform_count=1,
        source_communities=["r/freelance", "r/bookkeeping"],
        avg_comments=6.5,
        avg_votes=42.0,
        top_problem_statements=[{
            "post_id": "post-1",
            "problem_statement": "Freelancers waste hours chasing invoices.",
        }],
    )
    defaults.update(kw)
    return ClusterSignal(**defaults)


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
        permalink=None,
        author="freelancer42",
        score=120,
        num_comments=12,
        posted_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        scraped_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        raw_json={"upvote_ratio": 0.95, "top_comments": [{"body": "Same problem here"}]},
        content_hash=None,
        opportunity_type="software",
        opportunity_domain="fintech",
        problem_statement="Freelancers waste hours every week chasing unpaid invoices.",
        solution_angle="Automate invoice reminders and reconciliation for freelancers.",
        distance_to_centroid=0.1,
        similarity_score=0.91,
        assigned_by="model",
        model_version="v2",
    )
    defaults.update(kw)
    return ClusterItem(**defaults)


def make_snapshot(**kw):
    defaults = dict(
        id=1,
        cluster_id=42,
        date=date(2026, 5, 20),
        post_count=50,
        sample_posts=[],
        avg_comments=8.0,
    )
    defaults.update(kw)
    return ClusterSnapshot(**defaults)


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


def make_team(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        owner_user_id=TEST_UID,
        name="Immensity Research",
        description="Customer discovery workspace",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Team(**defaults)


def make_team_member(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
        user_id="member-uid",
        email="member@example.com",
        display_name="Member User",
        role="member",
        status="active",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return TeamMember(**defaults)


def make_issue(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        user_id=TEST_UID,
        team_id=None,
        assignee_id=None,
        pipeline_id=None,
        parent_issue_id=None,
        title="Analyze signals",
        summary="Review the signal page.",
        status="open",
        issue_type="issue",
        position=0,
        source="analyze_signals",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        closed_at=None,
    )
    defaults.update(kw)
    return Issue(**defaults)


def make_issue_comment(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        issue_id=str(uuid.uuid4()),
        user_id=TEST_UID,
        body="This needs deeper validation.",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return IssueComment(**defaults)


def make_usage_source(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        user_id=TEST_UID,
        public_key="usage_public_key",
        name="Website usage snippet",
        status="connected",
        product_url=None,
        allowed_domain=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        last_seen_at=None,
    )
    defaults.update(kw)
    return MonitorUsageSource(**defaults)


def make_usage_event(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        source_id=None,
        event_type="pageview",
        visitor_id="visitor-1",
        session_id="session-1",
        user_ref=None,
        url="https://example.com/",
        referrer=None,
        event_metadata={},
        occurred_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return MonitorUsageEvent(**defaults)


def make_error_group(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        source_id=str(uuid.uuid4()),
        fingerprint="fp-abc",
        title="TypeError: cannot read properties of undefined",
        level="error",
        status="unresolved",
        event_count=1,
        last_release=None,
        first_seen_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return MonitorErrorGroup(**defaults)


def make_error_event(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        source_id=None,
        group_id=None,
        fingerprint="fp-abc",
        message="TypeError: cannot read properties of undefined (reading 'x')",
        stack="at foo (app.js:10:5)\nat bar (app.js:20:7)",
        level="error",
        handled=False,
        url="https://example.com/app",
        release="v1.0.0",
        visitor_id="visitor-1",
        session_id="session-1",
        user_ref=None,
        event_metadata={},
        occurred_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return MonitorErrorEvent(**defaults)


def make_revenue_source(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        user_id=TEST_UID,
        provider="stripe",
        status="not_connected",
        account_mode="connected",
        provider_account_id=None,
        provider_account_label=None,
        current_mrr_cents=None,
        previous_mrr_cents=None,
        new_customers_30d=None,
        churned_customers_30d=None,
        churn_rate_30d=None,
        revenue_snapshot={},
        invoice_mrr_cents=None,
        previous_invoice_mrr_cents=None,
        invoice_revenue_snapshot={},
        connected_at=None,
        last_synced_at=None,
        oauth_state=None,
        oauth_state_expires_at=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return MonitorRevenueSource(**defaults)


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


def make_task(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        problem_id=None,
        user_id=TEST_UID,
        title="Draft the onboarding email",
        description=None,
        status="todo",
        position=0,
        due_date=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Task(**defaults)


def embedding_response(vector):
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
    application.include_router(clusters_router)
    application.include_router(search_sessions_router)
    application.include_router(signal_workspace_router)
    application.include_router(problems_router)
    application.include_router(pipeline_router)
    # Mirror production ordering: goals routes share the /portfolio prefix and
    # must be registered before the portfolio router's /{pipeline_id} catch-all.
    application.include_router(goals_router)
    application.include_router(portfolio_router)
    application.include_router(monitor_router, prefix="/portfolio")
    application.include_router(monitor_router, prefix="/monitor")
    application.include_router(monitor_war_room_router, prefix="/portfolio")
    application.include_router(monitor_war_room_router, prefix="/monitor")
    application.include_router(monitor_public_router, prefix="/public/portfolio")
    application.include_router(monitor_public_router, prefix="/public/monitor")
    application.include_router(tasks_router)
    application.include_router(teams_router)
    application.include_router(invites_router)
    application.include_router(issues_router)
    application.include_router(dashboard_router)
    application.include_router(homepage_router)
    application.include_router(public_router)
    application.include_router(preferences_router)

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
