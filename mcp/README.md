# Immensity Build MCP server

This local stdio server gives MCP-compatible coding agents project-scoped access to an approved Immensity Build Contract.

It exposes focused document reads, dependency-aware task selection, project status, blocker reporting, and verified completion. `get_project_context` remains available for clients that need the complete Problem Case and Build data in one call.

## Configuration

The server needs three environment variables:

- `IMMENSITY_API_URL`
- `IMMENSITY_PROJECT_ID`
- `IMMENSITY_PROJECT_TOKEN`

Generic MCP JSON:

```json
{
  "servers": {
    "immensity": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "immensity-mcp-server"],
      "env": {
        "IMMENSITY_API_URL": "https://api.example.com",
        "IMMENSITY_PROJECT_ID": "project-id",
        "IMMENSITY_PROJECT_TOKEN": "token-shown-once"
      }
    }
  }
}
```

Tokens are scoped to one project, stored only as hashes by the API, expire after 30 days, and can be revoked. The API rejects MCP access unless the token's exact Build Contract version is approved.
