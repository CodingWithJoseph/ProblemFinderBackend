from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

# Pipeline data version. The simplified ("v2") pipeline is the only version the
# API surfaces; every query that touches pipeline-produced tables filters on
# this value. Never hardcode the literal string — import this constant. Must
# match ProblemFinderAI's src/util/constants.py::PIPELINE_VERSION.
PIPELINE_VERSION = "v2"


class Settings(BaseSettings):
    # App
    app_name: str = "Immensity API"
    environment: str = "development"
    debug: bool = False

    # Supabase / Postgres
    database_url: str

    # Firebase
    firebase_project_id: str
    firebase_credentials_path: str = "firebase-service-account-prod.json"

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_pro_monthly_price_id: str
    stripe_pro_yearly_price_id: str
    stripe_elite_monthly_price_id: str
    stripe_elite_yearly_price_id: str
    stripe_connect_client_id: str = ""
    stripe_connect_redirect_url: str | None = None
    app_url: str = "http://localhost:3000"

    # OpenAI
    openai_api_key: str = ""

    # Anthropic (Claude) — opportunity "recommended next step" generation
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # Email
    # Transport selector: "console" logs the rendered message (default for dev
    # and tests, needs no credentials); "smtp" relays through the SMTP settings
    # below. Anything else falls back to console.
    email_transport: str = "console"
    email_from: str = "Immensity <no-reply@useimmensity.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    # How long a team invite link stays valid, in hours (default 7 days).
    invite_token_ttl_hours: int = 168

    # CORS
    allowed_origins: str = "http://localhost:3000"

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    class Config:
        env_file = Path(__file__).parent.parent / ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
