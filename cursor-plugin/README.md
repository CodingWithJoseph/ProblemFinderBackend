# Immensity for Cursor

Immensity connects Cursor to the hosted Immensity MCP server so the active Cursor model can work directly with a user's Immensity projects.

## What it adds

The plugin installs the production remote MCP endpoint:

`https://mcp.useimmensity.com/mcp`

Cursor handles OAuth through the MCP server's browser-based authorization flow. The plugin contains no embedded credentials or project-specific secrets.

## Workflow

Use Immensity from Cursor to:

- search for evidence-backed software problems
- create and select Immensity projects
- analyze project evidence with the active Cursor model and save the versioned Analysis
- design a Build with the active Cursor model and save the versioned Build
- inspect project status, implementation documents, and the next task
- track progress, blockers, and verification against the selected project

## Authentication

When Cursor first connects to Immensity, follow the browser sign-in and authorization flow. The MCP server uses the same production OAuth issuer and endpoint used by other supported MCP clients.
