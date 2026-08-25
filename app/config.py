from functools import lru_cache
from pathlib import Path

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

    @property
    def origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",")]

    @property
    def mcp_allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.mcp_allowed_hosts.split(",") if host.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()
