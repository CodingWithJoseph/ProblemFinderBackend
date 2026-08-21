"""Prepare real analyzed projects for website-to-MCP round-trip testing."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models import Pipeline, SignalAnalysisCase, SignalAnalysisVersion


LEGACY_SEED_PROVIDER = "local_seed"
LEGACY_SEED_MODEL = "build-ui-v1"


@dataclass(frozen=True)
class BuildTestExample:
    lane: str
    pipeline_id: str
    project_name: str
    analysis_version: int
    reused: bool = False


def require_development_environment(environment: str) -> None:
    if environment.strip().lower() in {"prod", "production"}:
        raise RuntimeError("Build round-trip examples can only be prepared outside production")


def assign_test_lanes(
    projects: list[tuple[Pipeline, SignalAnalysisCase, SignalAnalysisVersion]],
) -> list[BuildTestExample]:
    if not projects:
        raise RuntimeError("No real cluster-backed projects with completed Analysis were found")
    selected = projects[:2]
    if len(selected) == 1:
        selected.append(selected[0])
    result: list[BuildTestExample] = []
    for index, (pipeline, _, analysis) in enumerate(selected):
        result.append(BuildTestExample(
            lane="website" if index == 0 else "ide_mcp",
            pipeline_id=str(pipeline.id),
            project_name=pipeline.project_name or pipeline.name,
            analysis_version=int(analysis.version),
            reused=index == 1 and selected[0][0].id == pipeline.id,
        ))
    return result


async def remove_legacy_synthetic_project(db: AsyncSession, user_id: str) -> int:
    from app.models import Pipeline, SolutionCase, SolutionVersion

    rows = (await db.execute(
        select(Pipeline.id)
        .join(SolutionCase, SolutionCase.pipeline_id == Pipeline.id)
        .join(SolutionVersion, SolutionVersion.case_id == SolutionCase.id)
        .where(
            Pipeline.user_id == user_id,
            SolutionVersion.provider == LEGACY_SEED_PROVIDER,
            SolutionVersion.model == LEGACY_SEED_MODEL,
        )
    )).scalars().all()
    pipeline_ids = list(dict.fromkeys(str(value) for value in rows))
    if pipeline_ids:
        await db.execute(delete(Pipeline).where(Pipeline.id.in_(pipeline_ids)))
    return len(pipeline_ids)


async def find_real_analyzed_projects(
    db: AsyncSession,
    user_id: str,
    project_ids: list[str],
) -> list[tuple[Pipeline, SignalAnalysisCase, SignalAnalysisVersion]]:
    from app.models import Pipeline, SignalAnalysisCase, SignalAnalysisVersion

    query = (
        select(Pipeline, SignalAnalysisCase, SignalAnalysisVersion)
        .join(SignalAnalysisCase, SignalAnalysisCase.pipeline_id == Pipeline.id)
        .join(
            SignalAnalysisVersion,
            SignalAnalysisVersion.id == SignalAnalysisCase.current_version_id,
        )
        .where(
            Pipeline.user_id == user_id,
            Pipeline.removed_at == None,
            Pipeline.source_cluster_id != None,
            SignalAnalysisCase.user_id == user_id,
            SignalAnalysisCase.status == "ready",
        )
        .order_by(
            SignalAnalysisCase.analyzed_at.desc().nullslast(),
            Pipeline.updated_at.desc(),
        )
    )
    if project_ids:
        query = query.where(Pipeline.id.in_(project_ids))
    rows = list((await db.execute(query)).tuples().all())
    if project_ids:
        found = {str(row[0].id) for row in rows}
        missing = [project_id for project_id in project_ids if project_id not in found]
        if missing:
            raise RuntimeError(
                "These projects are not eligible real analyzed projects: " + ", ".join(missing)
            )
        order = {project_id: index for index, project_id in enumerate(project_ids)}
        rows.sort(key=lambda row: order[str(row[0].id)])
    return rows


async def prepare_examples(user_id: str, project_ids: list[str]) -> dict:
    from app.config import get_settings

    settings = get_settings()
    require_development_environment(settings.environment)
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        removed = await remove_legacy_synthetic_project(db, user_id)
        projects = await find_real_analyzed_projects(db, user_id, project_ids)
        examples = assign_test_lanes(projects)
        now = datetime.now(timezone.utc)
        unique_projects = {example.pipeline_id for example in examples}
        for pipeline, _, _ in projects:
            if str(pipeline.id) in unique_projects:
                pipeline.stage = "building"
                pipeline.updated_at = now
        await db.commit()
    return {
        "removedLegacySyntheticProjects": removed,
        "examples": [asdict(example) for example in examples],
        "nextStep": "Create a project token from Handoff for the ide_mcp example.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove the legacy synthetic Build seed and prepare two real, cluster-backed "
            "analyzed projects for website and IDE/MCP round-trip testing."
        ),
    )
    parser.add_argument("--user-id", required=True, help="Firebase UID that owns the projects")
    parser.add_argument(
        "--project-id",
        action="append",
        default=[],
        help="Optional eligible project ID. Pass once or twice to choose the examples.",
    )
    args = parser.parse_args()
    project_ids = list(dict.fromkeys(value.strip() for value in args.project_id if value.strip()))
    if len(project_ids) > 2:
        parser.error("--project-id can be supplied at most twice")
    result = asyncio.run(prepare_examples(args.user_id.strip(), project_ids))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
