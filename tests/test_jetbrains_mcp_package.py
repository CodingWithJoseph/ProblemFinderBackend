import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JETBRAINS_ROOT = ROOT / "jetbrains"
ENDPOINT = "https://mcp.useimmensity.com/mcp"


def _load(path: Path):
    return json.loads(path.read_text())


def test_junie_uses_production_remote_mcp():
    config = _load(JETBRAINS_ROOT / "junie" / "mcp.json")
    server = config["mcpServers"]["immensity"]

    assert server["url"] == ENDPOINT
    assert "headers" not in server
    assert "command" not in server


def test_ai_assistant_uses_same_remote_mcp_without_static_credentials():
    config = _load(JETBRAINS_ROOT / "ai-assistant" / "mcp.json")
    server = config["mcpServers"]["immensity"]

    assert server["url"] == ENDPOINT
    assert "headers" not in server
    assert "command" not in server


def test_official_registry_metadata_matches_jetbrains_distribution():
    registry = _load(ROOT / "server.json")

    assert registry["name"] == "io.github.CodingWithJoseph/immensity"
    assert registry["remotes"] == [
        {
            "type": "streamable-http",
            "url": ENDPOINT,
        }
    ]
