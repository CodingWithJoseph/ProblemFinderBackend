# Immensity backend

FastAPI backend for the Discovery, Problem Breakdown, and MVP Build warrooms.

## Release additions

- Versioned `solution-case.v1` contract
- Schema-constrained Solution Architect provider
- Durable solution-generation worker
- Immutable Solution Case versions
- Problem-unit and evidence traceability validation
- Project-scoped, revocable IDE access tokens
- Mutable task-progress overlays
- IDE context, next-task, and progress APIs
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

Publish the package as `immensity-mcp-server` before exposing the Connect IDE action to production users.

## Release smoke test

```bash
python scripts/smoke_release.py \
  --api-url https://api.useimmensity.com \
  --web-url https://console.useimmensity.com
```

The command checks API liveness/readiness plus the three canonical Warroom routes.
