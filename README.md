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
- Bidirectional website and IDE/MCP Build editing over one canonical version
- Publish-ready stdio MCP package in `mcp/`

## Run locally

The backend requires Python 3.12, PostgreSQL, and the local llama.cpp server.
Run the commands below from the repository root in PowerShell.

### 1. Install and configure the backend

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-test.txt
Copy-Item .env.example .env
```

Update `.env` with a working `DATABASE_URL` and Firebase credentials. The
checked-in development defaults use the OpenAI-compatible API at
`http://127.0.0.1:8080/v1` and the model alias `gpt-oss-20b`.

Apply the pending database migrations:

```powershell
python scripts\migrate.py
```

To prepare the two Build round-trip test lanes, run the local-only command with
the Firebase UID used to sign in to the web app:

```powershell
python scripts\prepare_build_roundtrip_examples.py --user-id <firebase-uid>
```

The command removes the old synthetic Build seed, selects up to two existing
cluster-backed projects with completed Analysis, and moves them to Build. If
only one eligible project exists, it is reused for both test lanes. It creates
no fake evidence or public demo route and refuses to run in production.

### 2. Start the local model

Start llama.cpp with the `gpt-oss-20b` alias and keep it running:

```powershell
& "C:\path\to\llama-server.exe" -hf ggml-org/gpt-oss-20b-GGUF --alias gpt-oss-20b --host 127.0.0.1 --port 8080 --ctx-size 32768 --jinja -ngl 99 -fa on
```

The backend uses one gateway and this model for Search, Analyze, Analyze
conversations, and Build. See [docs/ai-gateway.md](docs/ai-gateway.md) for the
shared development and production configuration.

### 3. Start the backend

In a second terminal, activate the virtual environment and start Uvicorn:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The API is available at `http://127.0.0.1:8000`, with interactive development
docs at `http://127.0.0.1:8000/docs`. Verify startup with:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Railway runs `python scripts/migrate.py` before every deployment and only marks the
service healthy after `/ready` confirms the database and release schema are ready.

## Test

```bash
pytest -q
```

## MCP

The hosted Streamable HTTP endpoint is available at `/mcp` on the existing API
domain. OAuth-capable clients open Immensity in the browser so the user can sign
in once, approve the connection, and select any project they own through the
`list_projects` and `select_project` tools. Project-scoped bearer tokens created
in Handoff remain available as a manual fallback.

In production, set `MCP_ALLOWED_HOSTS` to the API hostname, `MCP_PUBLIC_URL` to
the complete `/mcp` URL, `MCP_ISSUER_URL` to the API origin, and `WEB_APP_URL` to
the web app origin. Apply migration `0053_mcp_oauth.sql` before enabling OAuth.

The npm stdio package remains available as a local-development fallback:

```bash
cd mcp
npm install
npm run typecheck
npm run build
npm pack --dry-run
```

See [docs/build-workspace.md](docs/build-workspace.md) for the lifecycle and release contract.

## Release smoke test

```bash
python scripts/smoke_release.py \
  --api-url https://api.useimmensity.com \
  --web-url https://console.useimmensity.com
```

The command checks API liveness/readiness plus the three canonical Warroom routes.
