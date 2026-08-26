import pytest

from scripts.smoke_public_mcp import REQUIRED_TOOLS, origin_for


def test_smoke_harness_targets_the_public_mcp_origin():
    assert origin_for("https://mcp.immensity.ai/mcp") == "https://mcp.immensity.ai"


def test_smoke_harness_rejects_non_http_resource_urls():
    with pytest.raises(ValueError, match="absolute HTTP"):
        origin_for("mcp.immensity.ai/mcp")


def test_smoke_harness_requires_core_read_and_write_tools():
    assert {
        "list_projects",
        "select_project",
        "get_problem_case",
        "get_build_workspace",
        "get_build_contract",
        "get_next_task",
        "update_task_progress",
        "report_blocker",
        "submit_verification",
    }.issubset(REQUIRED_TOOLS)
