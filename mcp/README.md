# Immensity Build MCP server

This local stdio server is the manual development fallback for MCP-compatible agents. The preferred hosted connection uses the API's `/mcp` endpoint and browser OAuth, so users do not create or paste project tokens.

The fallback gives an agent project-scoped access to the same Problem Case and Build workspace used by the Immensity website.

During design, an agent can read the analyzed Problem Case, generate a Build draft, edit structured sections or editable documents, and approve an exact version. Every edit creates a canonical version that is immediately visible in the website. After approval, implementation tools expose focused document reads, dependency-aware task selection, project status, blocker reporting, and verified completion.

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

Tokens are scoped to one project, stored only as hashes by the API, expire after 30 days, and can be revoked. Design tools require a completed Analysis. Implementation tools additionally require the exact current Build version to be approved.

## Build design tools

- `get_problem_case` reads the approved Analyze output used as Build evidence.
- `get_build_workspace` reads the current canonical Build version.
- `generate_build_workspace` asks the configured backend model to create a draft.
- `update_build_section` replaces one structured section and creates a new draft version.
- `update_build_document` replaces one generated Markdown document and creates a new draft version.
- `approve_build_workspace` freezes the exact current version for implementation.

Always read the current workspace immediately before a write and pass its `version` as `expectedVersion`. A stale value returns a conflict instead of overwriting work saved in the website.

For `update_build_section`, send the complete replacement value in the same shape returned by `get_build_workspace`. Common examples:

```json
{
  "section": "mvp_goal",
  "expectedVersion": 3,
  "value": {
    "mvpGoal": "Clinicians can review an evidence-backed problem summary so that they can approve a focused first release.",
    "primaryUsers": ["Clinician"],
    "requiredCapability": "Review an evidence-backed problem summary",
    "successfulOutcome": "A focused first release is approved"
  }
}
```

```json
{
  "section": "delivery_targets",
  "expectedVersion": 4,
  "value": ["web", "ios"]
}
```

List sections such as `constraints`, `non_goals`, `features`, `tasks`, and `delivery_checklist` require their complete arrays. Object sections such as `primary_flow`, `architecture`, and `test_plan` require their complete objects. Every edit clears approval, so approve only after all design changes are complete.
