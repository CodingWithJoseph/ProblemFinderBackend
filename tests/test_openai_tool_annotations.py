from app.mcp_server import hosted_mcp_server


READ_ONLY_TOOLS = {
    "list_projects",
    "get_problem_case",
    "get_build_workspace",
    "get_build_contract",
    "list_build_documents",
    "get_build_document",
    "get_project_status",
    "get_next_task",
    "get_project_context",
}

NON_DESTRUCTIVE_WRITE_TOOLS = {"select_project"}

DESTRUCTIVE_WRITE_TOOLS = {
    "generate_build_workspace",
    "update_build_section",
    "update_build_document",
    "approve_build_workspace",
    "update_task_progress",
    "report_blocker",
    "submit_verification",
}


async def test_every_mcp_tool_has_openai_required_annotations():
    tools = {tool.name: tool for tool in await hosted_mcp_server.list_tools()}

    assert set(tools) == READ_ONLY_TOOLS | NON_DESTRUCTIVE_WRITE_TOOLS | DESTRUCTIVE_WRITE_TOOLS

    for name in READ_ONLY_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.open_world_hint is False

    for name in NON_DESTRUCTIVE_WRITE_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert annotations.open_world_hint is False

    for name in DESTRUCTIVE_WRITE_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is True
        assert annotations.open_world_hint is False
