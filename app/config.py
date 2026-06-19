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
    # and tests, needs no credentials); "resend" sends via the Resend API using
    # RESEND_API_KEY. Anything else falls back to console.
    email_transport: str = "console"
    email_from: str = "invites@useimmensity.com"
    resend_api_key: str = ""
    # How long a team invite link stays valid, in hours (default 7 days).
    invite_token_ttl_hours: int = 168

    # How often to auto-refresh connected Stripe revenue sources, in hours.
    # Set to 0 to disable the in-process scheduler.
    revenue_sync_interval_hours: int = 6

    # Alerting: scheduled checks email the product owner when something needs
    # attention. Real mail only sends when EMAIL_TRANSPORT is configured.
    alerts_enabled: bool = True
    alert_check_interval_hours: int = 1
    alert_error_spike_multiplier: float = 3.0
    alert_error_spike_min: int = 5
    alert_signups_drop_pct: float = 0.5
    alert_signups_min_previous: int = 5
    alert_revenue_drop_pct: float = 0.2

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
