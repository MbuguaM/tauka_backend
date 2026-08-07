from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    # ── AI providers ──────────────────────────────────────────────────────────
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    gemini_api_key: str = ""
    model_name: str = "deepseek-chat"
    token_limit: int = 100_000

    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str = ""
    supabase_key: str = ""          # service role key (env: SUPABASE_KEY)
    supabase_anon_key: str = ""     # anon key (env: SUPABASE_ANON_KEY)
    supabase_jwt_secret: str = ""

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"

    # ── Video calling ─────────────────────────────────────────────────────────
    daily_api_key: str = ""
    livekit_api_key: str = ""
    livekit_secret: str = ""

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_learner_monthly: str = ""
    stripe_price_tutor_monthly: str = ""
    stripe_price_intensive_monthly: str = ""
    stripe_price_gift_1mo: str = ""
    stripe_price_gift_3mo: str = ""

    # ── Email ─────────────────────────────────────────────────────────────────
    # Provider: "resend" (production), "smtp" (any SMTP host, incl. a local
    # Mailpit/MailHog catcher), or "console" (render to disk + log, no network).
    # "console" is the local default so a dev run never needs a provider account
    # and never risks mailing a real address from a test session.
    email_provider: str = "resend"
    email_api_key: str = ""
    email_from_address: str = "Tauka <hello@tauka.com>"

    # SMTP — used only when email_provider == "smtp".
    # Mailpit/MailHog: host=localhost, port=1025, no user/password, tls=False.
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False

    # Where the "console" provider writes rendered messages, relative to the
    # backend root. Each send lands as a timestamped .html you can open.
    email_outbox_dir: str = ".outbox"

    # ── App URLs ──────────────────────────────────────────────────────────────
    web_base_url: str = "https://www.tauka.com"
    app_base_url: str = "https://app.tauka.com"

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated browser origins allowed to call this API. The browser
    # sends a preflight OPTIONS for every request carrying
    # `Content-Type: application/json`, so without the matching origin listed
    # here every tauka-react-web call fails before it reaches a route — which
    # the web app reports as "the assessment service is unreachable".
    # Parsed as a plain string rather than list[str]: pydantic-settings expects
    # JSON for list-typed fields, and `a,b` in a .env would raise at startup.
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "https://www.tauka.com,https://tauka.com"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ── Encryption (payout settings at rest) ─────────────────────────────────
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    # ── Backward-compat uppercase properties (used by existing code) ──────────
    @property
    def OPENAI_API_KEY(self) -> str:
        return self.openai_api_key

    @property
    def DEEPSEEK_API_KEY(self) -> str:
        return self.deepseek_api_key

    @property
    def GEMINI_API_KEY(self) -> str:
        return self.gemini_api_key

    @property
    def MODEL_NAME(self) -> str:
        return self.model_name

    @property
    def TOKEN_LIMIT(self) -> int:
        return self.token_limit

    @property
    def SUPABASE_URL(self) -> str:
        return self.supabase_url

    @property
    def SUPABASE_KEY(self) -> str:
        return self.supabase_key

    @property
    def SUPABASE_JWT_SECRET(self) -> str:
        return self.supabase_jwt_secret

    @property
    def REDIS_URL(self) -> str:
        return self.redis_url

    @property
    def DAILY_API_KEY(self) -> str:
        return self.daily_api_key

    @property
    def LIVEKIT_API_KEY(self) -> str:
        return self.livekit_api_key

    @property
    def LIVEKIT_SECRET(self) -> str:
        return self.livekit_secret

    # Alias for service role key (spec uses supabase_service_key)
    @property
    def supabase_service_key(self) -> str:
        return self.supabase_key


settings = Settings()
