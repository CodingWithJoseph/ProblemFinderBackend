# Immensity for JetBrains

Immensity uses one hosted MCP endpoint across JetBrains clients:

`https://mcp.useimmensity.com/mcp`

## Junie

Junie supports remote HTTP MCP servers and browser-based OAuth authorization. Immensity is published under the official MCP Registry name `io.github.codingwithjoseph/immensity`, so the preferred installation path is Junie's MCP Installation Assistant once that Registry entry is live.

If direct configuration is needed, use `jetbrains/junie/mcp.json`. After adding the server, Junie should show `Authorization required`; choose **Authorize**, complete the Immensity browser sign-in flow, and verify the server becomes **Active**.

The same Junie MCP configuration is used by Junie CLI and Junie inside JetBrains IDEs.

## AI Assistant / AI Chat

JetBrains AI Assistant supports remote Streamable HTTP MCP servers, and its MCP configuration can be exposed to integrated agents through **Settings | Tools | AI Assistant | Agents | Pass custom MCP servers**. The exact Immensity HTTP configuration is stored in `jetbrains/ai-assistant/mcp.json`.

As of JetBrains AI Assistant 2026.2, the native AI Assistant MCP client does not yet complete OAuth authorization-code flows for remote MCP servers. Immensity intentionally does not provide a static credential fallback. Until JetBrains adds native MCP OAuth support, the supported JetBrains path is **Junie with Immensity OAuth**, including Junie when selected inside AI Chat.

Do not add long-lived access credentials to these configuration files.
