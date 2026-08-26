import pytest

from scripts.smoke_public_mcp import (
    DEFAULT_MCP_URL,
    REQUIRED_OAUTH_SCOPES,
    REQUIRED_TOOLS,
    origin_for,
)


def test_smoke_harness_targets_the_public_mcp_origin():
    assert DEFAULT_MCP_URL == "https://mcp.useimmensity.com/mcp"
    assert origin_for(DEFAULT_MCP_URL) == "https://mcp.useimmensity.com"


def test_smoke_harness_rejects_non_http_resource_urls():
    with pytest.raises(ValueError, match="absolute HTTP"):
        origin_for("mcp.useimmensity.com/mcp")


def test_smoke_harness_requires_openai_refresh_scope():
    assert REQUIRED_OAUTH_SCOPES == {"mcp:use", "offline_access"}


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
