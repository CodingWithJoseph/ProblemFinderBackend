# Build round-trip acceptance test

Use only projects selected by `scripts/prepare_build_roundtrip_examples.py`.
They must have a real source cluster and a completed current Analysis version.

## IDE/MCP to website

1. Create a project token from the project's Handoff screen and configure the MCP server.
2. Call `get_problem_case`, then `generate_build_workspace`.
3. Poll `get_build_workspace` until its status is `ready`.
4. Save one unmistakable constraint with `update_build_section`, using the returned version as `expectedVersion`.
5. Open or focus the same project in Build on the website.
6. Confirm the constraint and new version appear. Approve only after reviewing the complete draft.

## Website to IDE/MCP

1. Open the website example and generate its Build draft.
2. Edit and save one Brief section in the website.
3. Call `get_build_workspace` through MCP.
4. Confirm the exact saved value and version match the website.
5. Approve the current version, then confirm implementation tools return the approved documents.

## Pass conditions

- Both surfaces read and write the same versioned Build record.
- A stale `expectedVersion` returns a conflict instead of overwriting newer work.
- Every edit clears prior approval.
- Implementation tools remain unavailable until the exact current version is approved.
- No synthetic project, evidence, or public demo route is used.
