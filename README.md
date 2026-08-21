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

The backend requires Python 3.12, PostgreSQL, and a local OpenAI-compatible
model server. Run the commands below from the repository root in PowerShell.

### 1. Install and configure the backend

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-test.txt
Copy-Item .env.example .env
```

Update `.env` with a working `DATABASE_URL`, Firebase credentials, and any
other required secrets. The checked-in local model defaults already point to
`http://127.0.0.1:8080/v1` and use the model alias `gpt-oss-20b`.

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

### 2. Start the Llama agent

In the first terminal, start the local `gpt-oss-20b` model server. If
`llama-server.exe` is not on `PATH`, replace it with the full path to the
executable in your llama.cpp installation.

```powershell
llama-server.exe -hf ggml-org/gpt-oss-20b-GGUF --alias gpt-oss-20b --jinja --ctx-size 0 --host 127.0.0.1 --port 8080
```

Keep this terminal running. The backend uses its OpenAI-compatible API at
`http://127.0.0.1:8080/v1` for search interpretation, Signal analysis, and
Solution generation.

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
