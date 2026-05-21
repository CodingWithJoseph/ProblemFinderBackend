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
    firebase_credentials_path: str = "firebase-service-account.prod.json"

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str

    # CORS
    allowed_origins: str = "http://localhost:3000"

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    class Config:
        env_file = Path(__file__).parent.parent / ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()