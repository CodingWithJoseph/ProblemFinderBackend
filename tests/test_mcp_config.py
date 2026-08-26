import pytest
from pydantic import ValidationError

from app.config import Settings


def production_settings(**overrides):
    values = {
        "database_url": "postgresql+asyncpg://user:password@db.example.com/immensity",
        "firebase_project_id": "immensity-prod",
        "environment": "production",
        "allowed_origins": "https://console.useimmensity.com",
        "mcp_allowed_hosts": "mcp.immensity.ai",
        "mcp_public_url": "https://mcp.immensity.ai/mcp",
        "mcp_issuer_url": "https://mcp.immensity.ai",
        "web_app_url": "https://console.useimmensity.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_public_mcp_target_is_valid_for_production():
    settings = production_settings()

    assert settings.mcp_public_url == "https://mcp.immensity.ai/mcp"
    assert settings.mcp_issuer_url == "https://mcp.immensity.ai"
    assert settings.mcp_allowed_hosts_list == ["mcp.immensity.ai"]


def test_production_rejects_non_https_mcp_resource():
    with pytest.raises(ValidationError, match="MCP_PUBLIC_URL must be an HTTPS URL"):
        production_settings(mcp_public_url="http://mcp.immensity.ai/mcp")


def test_production_rejects_mcp_resource_without_mcp_path():
    with pytest.raises(ValidationError, match="public /mcp resource"):
        production_settings(mcp_public_url="https://mcp.immensity.ai/")


def test_production_requires_public_mcp_host_in_transport_allowlist():
    with pytest.raises(ValidationError, match="MCP_ALLOWED_HOSTS"):
        production_settings(mcp_allowed_hosts="api.useimmensity.com")
