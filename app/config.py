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
    # Customer-facing product name used in outbound copy (emails, etc.). Kept
    # separate from app_name (the API service name) so branding lives in one place.
    product_name: str = "Immensity"
    environment: str = "development"
    debug: bool = False
    feature_profile: str | None = None

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
    # How long a Stripe Connect OAuth state token stays valid, in minutes.
    stripe_oauth_state_ttl_minutes: int = 15
    app_url: str = "http://localhost:3000"

    # OpenAI search embeddings
    openai_api_key: str = ""

    # Optional OpenAI-compatible conversational search interpreter
    search_interpreter_provider: str = "local"
    search_interpreter_base_url: str = ""
    search_interpreter_model: str = ""
    search_interpreter_timeout_seconds: float = 30.0
    groq_api_key: str = ""

    # Optional, post-confirmation external evidence search. This stays off in
    # every environment unless explicitly enabled.
    search_web_enabled: bool = False
    search_web_model: str = "groq/compound-mini"
    search_web_timeout_seconds: float = 12.0
    search_summaries_enabled: bool = False

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
    # Lookback span for new-customer / churn revenue stats, in days.
    revenue_sync_window_days: int = 30
    # Which MRR engine the revenue serializer reads from. "subscription" is the
    # legacy "sum active subscriptions" snapshot; "invoice" is the invoice/event
    # ledger engine. Both are computed and stored every sync so totals can be
    # compared; this flag only decides which one is surfaced. Default stays on
    # the legacy path until the new engine is validated and the default flipped.
    revenue_engine: str = "subscription"
    # Base currency for cross-currency MRR totals, and a static rate table
    # (units of base per 1 unit of the keyed currency) used when an account
    # bills in more than one currency. No live FX in this PR; missing rates emit
    # a warning rather than silently mixing currencies.
    revenue_base_currency: str = "usd"
    revenue_fx_rates: dict[str, float] = {}
    # Default gross margin (0..1) for unit economics when a source hasn't set its
    # own; metrics computed with this default are badged "estimated".
    revenue_default_gross_margin_pct: float = 0.80

    # Analytics windows (days). These are the single source of truth for the
    # monitoring readouts; each API response echoes the window it used so the
    # frontend renders labels from data instead of hardcoding "14d"/"30d".
    analytics_usage_window_days: int = 14
    analytics_correlation_window_days: int = 30
    analytics_retention_window_days: int = 30
    analytics_growth_window_days: int = 7  # week-over-week comparison span

    # Alerting: scheduled checks email the product owner when something needs
    # attention. Real mail only sends when EMAIL_TRANSPORT is configured.
    alerts_enabled: bool = True
    alert_check_interval_hours: int = 1
    alert_error_spike_multiplier: float = 3.0
    alert_error_spike_min: int = 5
    alert_signups_drop_pct: float = 0.5
    alert_signups_min_previous: int = 5
    alert_revenue_drop_pct: float = 0.2
    # Days of history averaged into the error-spike baseline (excludes today).
    alert_error_baseline_days: int = 7
    # Signup comparison span: this window vs the immediately prior one.
    alert_signups_window_days: int = 7
    # Max new-issue emails emitted per run, so a burst can't blast an inbox.
    alert_new_issue_cap: int = 5

    # Ingest rate limit: max events accepted per source per minute (a batch
    # counts as its event total). Beyond this, ingest returns 429 and the beacon
    # backs off. In-process per worker; a coarse flood guard, not a precise quota.
    ingest_rate_limit_per_minute: int = 600

    # Health verdict engine (v0): a source is healthy while it's seen an event
    # within health_warning_hours, "quiet" up to health_unhealthy_hours, and
    # "silent" beyond it. Freshness-only for now; errors/vitals fold in at v2.
    health_warning_hours: int = 24
    health_unhealthy_hours: int = 72

    # Health verdict engine (v2): error rate (errors / sessions) thresholds that
    # tip a source into "noisy" then "failing". Vitals (poor LCP p75) and
    # freshness fold into the same verdict.
    health_error_rate_noisy: float = 0.1
    health_error_rate_failing: float = 0.3

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
