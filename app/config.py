from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

# Must match ProblemFinderAI's src/util/constants.py::PIPELINE_VERSION.
PIPELINE_VERSION = "v2"


class Settings(BaseSettings):
    app_name: str = "Immensity API"
    environment: str = "development"
    debug: bool = False

    database_url: str

    firebase_project_id: str
    firebase_credentials_path: str = "firebase-service-account-prod.json"

    openai_api_key: str = ""

    search_interpreter_provider: str = "local"
    search_interpreter_base_url: str = ""
    search_interpreter_model: str = ""
    search_interpreter_timeout_seconds: float = 30.0
    groq_api_key: str = ""

    signal_analysis_provider: str = ""
    signal_analysis_base_url: str = ""
    signal_analysis_model: str = ""
    signal_analysis_timeout_seconds: float = 90.0
    signal_analysis_max_evidence: int = 30
    signal_analysis_min_evidence: int = 3
    signal_analysis_worker_enabled: bool = True
    signal_analysis_poll_seconds: float = 2.0

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    allowed_origins: str = "http://localhost:3000"

    @property
    def origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",")]

    class Config:
        env_file = Path(__file__).parent.parent / ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
