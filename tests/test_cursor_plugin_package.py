import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "cursor-plugin"


def test_cursor_plugin_manifest_is_marketplace_ready():
    manifest = json.loads((PLUGIN_ROOT / ".cursor-plugin" / "plugin.json").read_text())

    assert manifest["name"] == "immensity"
    assert manifest["version"] == "1.0.0"
    assert manifest["author"]["name"] == "Immensity"
    assert manifest["homepage"] == "https://useimmensity.com"
    assert manifest["mcpServers"] == "mcp.json"
    assert "mcp" in manifest["keywords"]


def test_cursor_plugin_uses_only_the_production_remote_mcp():
    mcp = json.loads((PLUGIN_ROOT / "mcp.json").read_text())

    assert set(mcp["mcpServers"]) == {"immensity"}
    server = mcp["mcpServers"]["immensity"]
    assert server == {"url": "https://mcp.useimmensity.com/mcp"}


def test_cursor_plugin_does_not_embed_credentials():
    package_text = "\n".join(
        path.read_text()
        for path in PLUGIN_ROOT.rglob("*")
        if path.is_file()
    ).lower()

    assert "bearer " not in package_text
    assert "api_key" not in package_text
    assert "project_token" not in package_text
