# Build workspace foundation

Build turns one completed Problem Case into an implementation handoff. It does not add pricing, positioning, growth, market-fit scoring, deployment promises, or generic project-management scope.

## Source of truth

`SolutionCaseDocument` is the structured contract. Every saved generation or document edit creates an immutable `solution_versions` row. The current version remains a draft until the user explicitly approves that exact version.

Coding-agent access requires all of the following:

- the Problem Case and Build Contract are ready;
- `approval_status` is `approved`;
- `approved_version_id` equals `current_version_id`;
- the project token is active, unexpired, and scoped to the same project.

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
- `PATCH /pipeline/{id}/solution/case/documents/{key}`
- `POST /pipeline/{id}/solution/case/approve`
- `GET /pipeline/{id}/solution/case/export`
- `GET|POST|DELETE /pipeline/{id}/ide/access-tokens...`

Document edits and approval use `expectedVersion` to reject stale browser writes.

## Coding-agent API

Project tokens authorize:

- focused Build document listing and reads;
- full Problem Case and Build context for compatibility;
- project and blocker status;
- dependency-ready next-task selection;
- todo/in-progress updates;
- blocker reporting with a required note;
- completion only with a verification summary and checks that passed.

The stdio MCP package maps these routes to eight focused tools. Its server instructions are intentionally short so clients that truncate MCP instructions still receive the core rules.

## Release requirements

1. Apply migration `0052_build_contract_foundation`.
2. Deploy the backend and frontend together because the response contract adds documents and approval state.
3. Publish `mcp/` as `immensity-mcp-server@0.2.0` before exposing `npx` setup in production.
4. Verify `/ready`, then run backend tests, frontend tests/build, MCP typecheck/build, and a stdio `initialize` + `tools/list` smoke test.
