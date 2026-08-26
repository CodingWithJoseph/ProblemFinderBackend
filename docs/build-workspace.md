# Build workspace foundation

Build turns one completed Problem Case into an implementation handoff. It does not add pricing, positioning, growth, market-fit scoring, deployment promises, or generic project-management scope.

## Source of truth

`SolutionCaseDocument` is the structured contract. Every saved generation or document edit creates an immutable `solution_versions` row. The current version remains a draft until the user explicitly approves that exact version.

Build-design access requires a real project with a completed current Problem
Case. Website and IDE/MCP design edits write new immutable versions of the same
canonical Build record, and every write includes the expected current version.

Implementation access requires all of the following:

- the Problem Case and Build Contract are ready;
- `approval_status` is `approved`;
- `approved_version_id` equals `current_version_id`;
- the coding-agent authorization is active and the selected project belongs to the signed-in user.

Saving an edit creates a new version and clears approval. Regeneration also clears approval and replaces task-progress overlays because it may replace the task graph.

## Generated package

The focused package contains:

- `docs/BUILD_CONTRACT.md`
- `docs/FEATURES.md`
- `docs/REQUIREMENTS.md`
- `docs/TASKS.md`
- `docs/TEST_PLAN.md`
- `docs/DECISIONS.md`
- `AGENTS.md`
- `CLAUDE.md`
- `.github/copilot-instructions.md`
- `build.json` in ZIP exports

Agent instruction files are read-only derived views. User-editable Markdown is stored with the immutable version, so MCP and downloads return the same approved content. Build Contract, feature, requirement, task, and test-plan edits are parsed back into the structured contract; feature and requirement acceptance criteria stay synchronized. Malformed provenance, IDs, dependencies, or required sections are rejected instead of allowing Markdown and MCP data to drift.

## User API

- `GET /pipeline/{id}/solution/case`
- `POST /pipeline/{id}/solution/case/generate`
- `PATCH /pipeline/{id}/solution/case/sections/{key}`
- `PATCH /pipeline/{id}/solution/case/documents/{key}`
- `POST /pipeline/{id}/solution/case/approve`
- `GET /pipeline/{id}/solution/case/export`
- `GET|POST|DELETE /pipeline/{id}/ide/access-tokens...`

Document edits and approval use `expectedVersion` to reject stale browser writes.

## Coding-agent API

Browser-approved OAuth connections authorize Build design through:

- current Problem Case and Build workspace reads;
- backend-model Build generation;
- structured section and editable document writes;
- explicit approval of the exact current version.

Once approved, the same connection authorizes implementation through:

- focused Build document listing and reads;
- full Problem Case and Build context for compatibility;
- project and blocker status;
- dependency-ready next-task selection;
- todo/in-progress updates;
- blocker reporting with a required note;
- completion only with a verification summary and checks that passed.

The hosted `/mcp` Streamable HTTP endpoint exposes seventeen focused tools: two account project-selection tools and fifteen Build tools. OAuth-capable clients use dynamic registration, PKCE, and browser approval. Existing project tokens remain a project-scoped manual fallback, and the stdio npm package remains available for local development. Server instructions are intentionally short so clients that truncate MCP instructions still receive the core rules.

## Release requirements

1. Apply migrations through `0053_mcp_oauth`.
2. Deploy the backend and frontend together because the response contract adds documents and approval state.
3. Set `MCP_ALLOWED_HOSTS`, `MCP_PUBLIC_URL`, `MCP_ISSUER_URL`, and `WEB_APP_URL`; then verify OAuth discovery, browser approval, `/mcp` initialization, and `tools/list`.
4. Publish `mcp/` as `immensity-mcp-server@0.3.0` only when the optional local `npx` setup is promoted.
5. Verify `/ready`, then run backend tests, frontend tests/build, MCP typecheck/build, and the remote MCP smoke test.
