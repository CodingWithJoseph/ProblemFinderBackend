# Public MCP architecture

Immensity uses one account-scoped OAuth-backed MCP service for coding agents and IDE integrations.

## Connection flow

1. The client connects to the public Immensity MCP endpoint.
2. The MCP server starts the OAuth authorization flow.
3. The user signs in to Immensity and approves access.
4. The client receives an OAuth access token and refresh token.
5. The agent calls `list_projects`.
6. The agent calls `select_project` when more than one project is available.
7. All Build and implementation tools operate against the selected project.

The authorization server advertises both `mcp:use` and `offline_access`. `mcp:use` is required for the MCP resource; `offline_access` allows clients that request it to maintain connectivity with refresh tokens after the short-lived access token expires.

Manual project tokens are not part of the supported architecture. The server must not accept legacy `imm_mcp_*` credentials or expose create/list/revoke endpoints for them.

## Shared MCP tool surface

The public service exposes the same tool contract to ChatGPT/Codex, JetBrains, and future clients:

- `list_projects`
- `select_project`
- `get_problem_case`
- `get_build_workspace`
- `generate_build_workspace`
- `update_build_section`
- `update_build_document`
- `approve_build_workspace`
- `get_build_contract`
- `list_build_documents`
- `get_build_document`
- `get_project_status`
- `get_next_task`
- `update_task_progress`
- `report_blocker`
- `submit_verification`
- `get_project_context`

Client-specific plugins or integrations should be thin wrappers around this shared service. They must not create separate MCP backends.

## Production target

The public resource URL is `https://mcp.useimmensity.com/mcp`. Production deployment, DNS, TLS, and environment configuration are handled separately from the OAuth-only application architecture.
