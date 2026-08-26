# Public MCP deployment

Target resource: `https://mcp.immensity.ai/mcp`

The backend already mounts the Streamable HTTP MCP application at `/mcp`. The public hostname should route to the same Railway service that runs the Immensity FastAPI application.

## Railway environment

Set these values in the production service:

```text
ENVIRONMENT=production
MCP_ALLOWED_HOSTS=mcp.immensity.ai
MCP_PUBLIC_URL=https://mcp.immensity.ai/mcp
MCP_ISSUER_URL=https://mcp.immensity.ai
WEB_APP_URL=https://console.useimmensity.com
ALLOWED_ORIGINS=https://console.useimmensity.com
```

Keep the existing production `DATABASE_URL`, Firebase credentials, AI provider settings, and worker settings unchanged.

## Database

Run the normal migration command before exposing the public hostname:

```text
python scripts/migrate.py
```

The database must include the OAuth tables introduced by `0053_mcp_oauth.sql` and the legacy manual-token table removal in `0054_drop_legacy_ide_access_tokens.sql`.

## DNS and TLS

1. Add `mcp.immensity.ai` as a custom domain for the Railway backend service.
2. Add the DNS record Railway requests for that custom domain.
3. Wait for Railway to provision a valid TLS certificate.
4. Do not enable the hostname for clients until `https://mcp.immensity.ai/ready` and OAuth discovery are reachable over HTTPS.

## Required public checks

After DNS and TLS are live, verify:

```text
GET  https://mcp.immensity.ai/health
GET  https://mcp.immensity.ai/ready
GET  https://mcp.immensity.ai/.well-known/oauth-authorization-server
GET  https://mcp.immensity.ai/.well-known/oauth-protected-resource/mcp
POST https://mcp.immensity.ai/mcp
```

The unauthenticated MCP POST should return an authentication challenge rather than application data.

## OAuth browser flow

A client should be able to dynamically register, start authorization, redirect the user to the Immensity web application, exchange the returned authorization code, refresh the access token, call `list_projects`, and select a project.

Do not publish ChatGPT/Codex or JetBrains integration instructions until this end-to-end public flow passes.

## Repository versus infrastructure

This repository can validate production configuration and provide the public MCP application, but Railway custom-domain settings and DNS records are infrastructure changes outside the repository. Those must be applied in Railway and the DNS provider before the hostname is actually public.
