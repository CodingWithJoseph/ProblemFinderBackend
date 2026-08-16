# Immensity backend

FastAPI backend for the Discovery, Analysis, and Build workflow.

## Release additions

- Versioned `solution-case.v1` Build Contract
- Schema-constrained Solution Architect provider
- Durable solution-generation worker
- Immutable Solution Case versions
- Problem-unit and evidence traceability validation
- Project-scoped, revocable IDE access tokens
- Focused Markdown package, immutable editing, approval, and ZIP export
- Mutable task progress, blocker, and verification overlays
- Focused document, project-status, context, and next-task APIs
- Publish-ready stdio MCP package in `mcp/`

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt -r requirements-test.txt
uvicorn main:app --reload
```

Railway runs `python scripts/migrate.py` before every deployment and only marks the
service healthy after `/ready` confirms the database and release schema are ready.

## Test

```bash
pytest -q
```

## MCP package

```bash
cd mcp
npm install
npm run typecheck
npm run build
npm pack --dry-run
```

Publish the package as `immensity-mcp-server@0.2.0` before exposing the coding-agent action to production users. See [docs/build-workspace.md](docs/build-workspace.md) for the lifecycle and release contract.

## Release smoke test

```bash
python scripts/smoke_release.py \
  --api-url https://api.useimmensity.com \
  --web-url https://console.useimmensity.com
```

The command checks API liveness/readiness plus the three canonical Warroom routes.
