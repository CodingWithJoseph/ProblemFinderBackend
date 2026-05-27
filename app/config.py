from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

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
    app_url: str = "http://localhost:3000"

    # OpenAI
    openai_api_key: str = ""

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