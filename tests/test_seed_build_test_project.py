import uuid
from datetime import datetime, timezone

import pytest

from scripts.seed_build_test_project import (
    build_test_document,
    require_local_environment,
    test_project_ids as build_test_project_ids,
)


def test_build_test_document_exercises_every_build_workspace() -> None:
    generated_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
    document = build_test_document("firebase-user-1", generated_at)

    assert document.status == "ready"
    assert document.version == 1
    assert document.project.project_name == "Invoice Reminder"
    assert len(document.features) == 2
    assert len(document.tasks) == 3
    assert document.architecture is not None
    assert document.test_plan is not None
    assert [item.filename for item in document.documents] == [
        "docs/BUILD_CONTRACT.md",
        "docs/FEATURES.md",
        "docs/REQUIREMENTS.md",
        "docs/TASKS.md",
        "docs/TEST_PLAN.md",
        "docs/DECISIONS.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".github/copilot-instructions.md",
    ]


def test_build_test_project_ids_are_stable_and_user_scoped() -> None:
    first = build_test_project_ids("firebase-user-1")

    assert first == build_test_project_ids("firebase-user-1")
    assert first != build_test_project_ids("firebase-user-2")
    assert all(value == str(uuid.UUID(value)) for value in first)


def test_build_test_seed_refuses_production() -> None:
    with pytest.raises(RuntimeError, match="cannot be seeded in production"):
        require_local_environment("production")

    require_local_environment("development")
