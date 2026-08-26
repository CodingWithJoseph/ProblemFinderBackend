from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Must match ProblemFinderAI's src/util/constants.py::PIPELINE_VERSION.
PIPELINE_VERSION = "v2"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        extra="ignore",
    )

    app_name: str = "Immensity API"
    environment: str = "development"
    debug: bool = False

    database_url: str

    firebase_project_id: str
    firebase_credentials_path: str = "firebase-service-account-prod.json"

    ai_provider: str = "local"
    ai_base_url: str = ""
    ai_model: str = ""
    ai_api_key: str = ""
    ai_timeout_seconds: float = 120.0
    ai_reasoning_effort: str = "medium"

    signal_analysis_max_evidence: int = 30
    signal_analysis_min_evidence: int = 3
    signal_analysis_worker_enabled: bool = True
    signal_analysis_poll_seconds: float = 2.0

    solution_max_output_tokens: int = 10_000
    solution_worker_enabled: bool = True

    allowed_origins: str = "http://localhost:3000"
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*,testserver"
    mcp_public_url: str = "http://127.0.0.1:8000/mcp"
    mcp_issuer_url: str = "http://127.0.0.1:8000"
    web_app_url: str = "http://localhost:3000"

    @property
    def origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",")]

    @property
    def mcp_allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.mcp_allowed_hosts.split(",") if host.strip()]

    @model_validator(mode="after")
    def validate_production_mcp_configuration(self) -> "Settings":
        if self.environment.lower() != "production":
            return self

        public = urlparse(self.mcp_public_url)
        issuer = urlparse(self.mcp_issuer_url)
        web = urlparse(self.web_app_url)

        if public.scheme != "https" or not public.hostname:
            raise ValueError("MCP_PUBLIC_URL must be an HTTPS URL in production")
        if public.path.rstrip("/") != "/mcp":
            raise ValueError("MCP_PUBLIC_URL must point to the public /mcp resource")
        if issuer.scheme != "https" or not issuer.hostname:
            raise ValueError("MCP_ISSUER_URL must be an HTTPS origin in production")
        if issuer.path not in ("", "/"):
            raise ValueError("MCP_ISSUER_URL must be an origin without a path")
        if web.scheme != "https" or not web.hostname:
            raise ValueError("WEB_APP_URL must be an HTTPS origin in production")
        if web.path not in ("", "/"):
            raise ValueError("WEB_APP_URL must be an origin without a path")

        allowed_hosts = {
            host.split(":", 1)[0].lower()
            for host in self.mcp_allowed_hosts_list
            if host and not host.startswith("*")
        }
        if public.hostname.lower() not in allowed_hosts:
            raise ValueError("MCP_ALLOWED_HOSTS must include the MCP_PUBLIC_URL hostname")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
