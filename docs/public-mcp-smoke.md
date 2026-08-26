# Public MCP smoke validation

Run these checks only after `https://mcp.useimmensity.com` has valid DNS/TLS and the production environment values from `docs/public-mcp-deployment.md` are active.

## 1. Public unauthenticated checks

```bash
python scripts/smoke_public_mcp.py
```

This verifies:

- `/health`
- `/ready`
- OAuth authorization-server metadata, including `mcp:use` and `offline_access`
- OAuth protected-resource metadata for `/mcp`
- unauthenticated MCP requests return an OAuth challenge

No credentials or project data are required.

## 2. Authenticated MCP tool checks

Obtain a valid Immensity OAuth access token through an OAuth-capable MCP client, then run:

```bash
python scripts/smoke_public_mcp.py \
  --access-token "$IMMENSITY_MCP_ACCESS_TOKEN"
```

This opens a real Streamable HTTP MCP session, verifies the full required tool surface, and calls `list_projects`.

For the client-connection test, request both `mcp:use` and `offline_access`, verify that the token response includes a refresh token, and confirm the refresh-token grant can issue a replacement access token.

## 3. Approved-project read flow

Use a dedicated approved test project and pass its ID:

```bash
python scripts/smoke_public_mcp.py \
  --access-token "$IMMENSITY_MCP_ACCESS_TOKEN" \
  --project-id "<pipeline-id>"
```

The script then calls:

- `select_project`
- `get_problem_case`
- `get_build_workspace`
- `get_build_contract`
- `get_project_status`
- `get_next_task`

The script also verifies that `update_task_progress`, `report_blocker`, and `submit_verification` are advertised by the server, but intentionally does not invoke them because those tools mutate project state.

## Gate for client integrations

Do not start ChatGPT/Codex or JetBrains distribution work until all three smoke lanes pass against the real public endpoint. Use a dedicated test project for the approved-project lane so validation never depends on customer/project production state.
