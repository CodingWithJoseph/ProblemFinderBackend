# Immensity MCP server

This local stdio server gives an IDE agent project-scoped access to a generated MVP solution. It exposes three tools:

- `get_project_context`
- `get_next_task`
- `update_task_progress`

Create a token in the MVP Build Warroom, then configure an MCP-compatible IDE:

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

Tokens are scoped to one project, stored as hashes by the API, expire after 30 days, and can be revoked from the product.
