"""Getting-started guide.

The tier-based Goal Progress Engine (app/services/goals.py) tracks *numeric
outcomes* — signups, revenue, launches — and only lights up once real data
flows. That leaves a brand-new account with nothing to act on. This module fills
that gap with a small, ordered onboarding sequence that teaches the product's
landscape (discover → build → launch → monitor) by pointing at the next concrete
action to take.

Steps are heterogeneous booleans (has a pipeline item? has launched? …), not one
metric's tiers, so they live beside the tier engine rather than inside it — and
they need no new tables: each step is a predicate over existing rollups. The
payload powers both the Goals-page checklist and the Timeline's "do this next"
hint, so the two surfaces read from one source and stay in sync.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    MonitorUsageSource,
    Pipeline,
    Problem,
    Task,
)

# Ordered onboarding steps. ``routeKey`` maps to a frontend route (app/util/routes
# → routes.core[routeKey]) so the client can deep-link "do this next" without the
# backend knowing URLs. Order is the funnel a new user should follow.
STEPS: list[dict[str, str]] = [
    {
        "key": "save_pipeline",
        "title": "Save a product idea",
        "description": "Add a signal you like to the pipeline — that's the project everything else hangs off.",
        "actionLabel": "Open the pipeline",
        "routeKey": "pipeline",
    },
    {
        "key": "discover_problem",
        "title": "Break it into problems",
        "description": "Turn the idea into the specific problems worth solving.",
        "actionLabel": "Open discovery",
        "routeKey": "problems",
    },
    {
        "key": "create_task",
        "title": "Turn problems into tasks",
        "description": "Give a problem a concrete next step so it shows up on your timeline.",
        "actionLabel": "Open tasks",
        "routeKey": "tasks",
    },
    {
        "key": "launch_product",
        "title": "Launch your product",
        "description": "Mark a product launched to start tracking its real-world traction.",
        "actionLabel": "Open the portfolio",
        "routeKey": "portfolio",
    },
    {
        "key": "connect_data",
        "title": "Connect product data",
        "description": "Wire up the usage beacon so signups and traffic feed your goals automatically.",
        "actionLabel": "Open monitoring setup",
        "routeKey": "monitorSetup",
    },
]


def _exists(select_stmt):
    """Narrow an existence query to a single column limited to one row."""
    return select_stmt.limit(1)


async def _step_completion(uid: str, db: AsyncSession) -> dict[str, bool]:
    """Whether each step is done, evaluated in ``STEPS`` order — one existence
    query per step against existing tables (no goal state of its own)."""
    save_pipeline = bool(await db.scalar(
        _exists(select(Pipeline.id).where(Pipeline.user_id == uid, Pipeline.removed_at == None))
    ))
    discover_problem = bool(await db.scalar(
        _exists(select(Problem.id).where(Problem.user_id == uid))
    ))
    create_task = bool(await db.scalar(
        _exists(select(Task.id).where(Task.user_id == uid))
    ))
    launch_product = bool(await db.scalar(
        _exists(select(Pipeline.id).where(
            Pipeline.user_id == uid, Pipeline.launched_at != None, Pipeline.removed_at == None
        ))
    ))
    connect_data = bool(await db.scalar(
        _exists(select(MonitorUsageSource.id).where(
            MonitorUsageSource.user_id == uid, MonitorUsageSource.status == "connected"
        ))
    ))
    return {
        "save_pipeline": save_pipeline,
        "discover_problem": discover_problem,
        "create_task": create_task,
        "launch_product": launch_product,
        "connect_data": connect_data,
    }


async def getting_started_payload(uid: str, db: AsyncSession) -> dict:
    """The onboarding checklist plus the single next action to take.

    ``nextStep`` is the first *incomplete* step in funnel order — the "do this
    next" the UI surfaces — or ``None`` once every step is done."""
    done = await _step_completion(uid, db)
    steps = [
        {
            "key": s["key"],
            "title": s["title"],
            "description": s["description"],
            "actionLabel": s["actionLabel"],
            "routeKey": s["routeKey"],
            "done": done.get(s["key"], False),
        }
        for s in STEPS
    ]
    next_step = next((s for s in steps if not s["done"]), None)
    completed = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "completedCount": completed,
        "totalCount": len(steps),
        "complete": next_step is None,
        "nextStep": None if next_step is None else {
            "key": next_step["key"],
            "title": next_step["title"],
            "description": next_step["description"],
            "actionLabel": next_step["actionLabel"],
            "routeKey": next_step["routeKey"],
        },
    }
