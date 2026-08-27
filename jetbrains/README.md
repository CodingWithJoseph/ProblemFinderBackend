# Immensity for JetBrains

Immensity uses one hosted MCP endpoint across JetBrains clients:

`https://mcp.useimmensity.com/mcp`

## Junie

Junie supports remote HTTP MCP servers and browser-based OAuth authorization. Immensity is published under the official MCP Registry name `io.github.CodingWithJoseph/immensity`, so the preferred installation path is Junie's MCP Installation Assistant when the Registry entry is available in the client.

If direct configuration is needed, use `jetbrains/junie/mcp.json`. After adding the server, Junie should show that authentication is required; choose **Authorize**, complete the Immensity browser sign-in flow, and verify the server becomes active.

The same hosted Immensity MCP endpoint is used by Junie CLI and Junie inside JetBrains IDEs.

## AI Assistant / AI Chat

JetBrains AI Assistant supports remote Streamable HTTP MCP servers, and configured MCP servers can be exposed to integrated agents through **Settings | Tools | AI Assistant | Agents | Pass custom MCP servers**. The exact Immensity HTTP configuration is stored in `jetbrains/ai-assistant/mcp.json`.

There is also an agent-native path for AI Chat. If Immensity has already been installed and authorized in a supported agent such as Codex, selecting that same agent in JetBrains AI Chat can make the agent's Immensity MCP tools available there as well. This provides a useful fallback when JetBrains' own MCP authorization UI is unavailable or fails to launch the browser. Keep OAuth credentials in the agent's normal credential store; do not paste bearer tokens into JetBrains configuration files.

For Codex, Immensity can be registered against the same hosted endpoint and authorized with Codex's OAuth flow, then used when **Codex** is selected in JetBrains AI Chat. JetBrains also supports passing MCP servers configured in AI Assistant directly to Codex and Claude Agent through the **Pass custom MCP servers** setting.

Do not add long-lived access credentials to these configuration files.
