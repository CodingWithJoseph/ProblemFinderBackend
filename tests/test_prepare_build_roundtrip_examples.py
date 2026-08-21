from datetime import datetime, timezone
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import Pipeline, SignalAnalysisCase, SignalAnalysisVersion
from scripts.prepare_build_roundtrip_examples import (
    assign_test_lanes,
    require_development_environment,
)


def _project(name: str, pipeline_id: str, version: int = 1):
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    pipeline = Pipeline(
        id=pipeline_id,
        user_id="user-1",
        name=name,
        project_name=None,
        post_ids=["source-1"],
        source_cluster_id="42",
        stage="validating",
        created_at=now,
        updated_at=now,
    )
    case = SignalAnalysisCase(
        id=f"case-{pipeline_id}",
        pipeline_id=pipeline_id,
        user_id="user-1",
        status="ready",
        current_version_id=f"version-{pipeline_id}",
        created_at=now,
        updated_at=now,
    )
    analysis = SignalAnalysisVersion(
        id=f"version-{pipeline_id}",
        case_id=case.id,
        version=version,
        schema_version="signal-case.v1",
        provider="local",
        model="real-analysis-model",
        source_fingerprint="fingerprint",
        analysis={"problemThesis": {"statement": "Real analyzed complaint cluster"}},
        generated_at=now,
    )
    return pipeline, case, analysis


def test_two_real_projects_are_assigned_to_separate_round_trip_lanes() -> None:
    lanes = assign_test_lanes([_project("First", "pipeline-1"), _project("Second", "pipeline-2", 3)])

    assert [lane.lane for lane in lanes] == ["website", "ide_mcp"]
    assert [lane.pipeline_id for lane in lanes] == ["pipeline-1", "pipeline-2"]
    assert lanes[1].analysis_version == 3
    assert lanes[1].reused is False


def test_one_real_project_can_be_reused_for_both_round_trip_lanes() -> None:
    lanes = assign_test_lanes([_project("Only real project", "pipeline-1")])

    assert lanes[0].pipeline_id == lanes[1].pipeline_id
    assert lanes[0].reused is False
    assert lanes[1].reused is True


def test_round_trip_preparation_refuses_production() -> None:
    with pytest.raises(RuntimeError, match="outside production"):
        require_development_environment("production")

    require_development_environment("development")


def test_preparation_script_can_be_run_directly() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "scripts/prepare_build_roundtrip_examples.py", "--help"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "round-trip testing" in result.stdout
