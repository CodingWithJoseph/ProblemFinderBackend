import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "openai-plugin"


def test_openai_plugin_manifest_contains_public_listing_metadata():
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())

    assert manifest["name"] == "immensity"
    assert manifest["interface"]["displayName"] == "Immensity"
    assert manifest["interface"]["websiteURL"] == "https://useimmensity.com"
    assert manifest["interface"]["privacyPolicyURL"] == "https://useimmensity.com/legal/privacy"
    assert manifest["interface"]["termsOfServiceURL"] == "https://useimmensity.com/legal/terms"
    assert manifest["interface"]["capabilities"] == ["Read", "Write"]
    assert len(manifest["interface"]["defaultPrompt"]) >= 1


def test_openai_submission_materials_cover_required_review_cases():
    submission = json.loads((PLUGIN_ROOT / "submission.json").read_text())

    assert submission["submissionType"] == "With MCP"
    assert submission["mcp"]["urlType"] == "Universal"
    assert submission["mcp"]["serverUrl"] == "https://mcp.useimmensity.com/mcp"
    assert len(submission["positiveTestCases"]) == 5
    assert len(submission["negativeTestCases"]) == 3
    assert submission["listing"]["supportUrl"] == "https://useimmensity.com/contact"
