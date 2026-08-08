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
    # Must be the authenticated SMTP mailbox or a verified alias of it. Zoho (and
    # most hosts) accept any sender at MAIL FROM and then reject at DATA with
    # "553 Sender is not allowed to relay emails", so a mismatch here fails every
    # send while looking fine at connection time.
    email_from_address: str = "Tauka <support@tauka.io>"

    # Internal recipient for operational notifications (new testimonial, etc).
    # Unlike email_from_address this is a plain address, not a display-name form —
    # it is only ever a recipient. Empty disables those notifications outright.
    admin_email: str = "support@tauka.io"

    # SMTP — used only when email_provider == "smtp".
    # Mailpit/MailHog: host=localhost, port=1025, no user/password, no TLS.
    # A real host wants exactly one of the two TLS modes, chosen by port:
    #   port 587 → smtp_starttls=True   (connect plaintext, upgrade with STARTTLS)
    #   port 465 → smtp_ssl=True        (TLS from the first byte; STARTTLS is
    #                                    not offered and is skipped if also set)
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    smtp_ssl: bool = False

    # Where the "console" provider writes rendered messages, relative to the
    # backend root. Each send lands as a timestamped .html you can open.
    email_outbox_dir: str = ".outbox"

    # Socket timeout for a single SMTP send. Was hard-coded at 15s, which is
    # longer than the ENTIRE function budget on Vercel Hobby (10s) — a stalled
    # send would be killed by the platform mid-connection with no response
    # returned at all, rather than failing cleanly and letting the request
    # finish. Keep this comfortably below the platform limit.
    smtp_timeout_seconds: int = 8

    # ── Follow-up work ────────────────────────────────────────────────────────
    # Ceiling for post-request effects (emails, referral linkage, usage logging)
    # now that they run inline — see app/core/followup.py. Sized for Vercel
    # Hobby's 10s function limit with room for the request itself; raise it on
    # Pro (60s default) or on any persistent host.
    followup_timeout_seconds: float = 6.0

    # ── Scheduled jobs ────────────────────────────────────────────────────────
    # Shared secret for GET /tasks/daily. Vercel Cron sends it automatically as
    # `Authorization: Bearer $CRON_SECRET` when the env var of that name is set
    # on the project. Empty means the endpoint refuses every request — it fails
    # CLOSED, because an open endpoint here would let anyone trigger mail to
    # every supporter you have.
    cron_secret: str = ""

    # ── App URLs ──────────────────────────────────────────────────────────────
    # Both are injected into every email template as Jinja globals, so a wrong
    # value here silently ships dead links in mail already sent.
    #
    # There is deliberately no app_base_url. Everything this service links to is
    # a browser destination — Stripe success/cancel/return targets and email
    # CTAs — and account management lives on the web by design; the Flutter app
    # is for usage only. The setting used to default to https://app.tauka.com,
    # a host that exists nowhere in the system (the app's real deep-link host is
    # app.tauka.app, for tauka:// universal links, which Stripe cannot redirect
    # a browser to anyway). Use web_base_url.
    web_base_url: str = "https://tauka.io"

    # This service's own public origin. Needed because the unsubscribe and
    # milestone opt-out links in email resolve to FastAPI routes here
    # (GET /supporters/unsubscribe/{id}, /supporters/opt-out/{id}, both
    # HTMLResponse), NOT to pages on the marketing site. Templates built those
    # two links from web_base_url, which 404s on the React router.
    api_base_url: str = "https://api.tauka.io"

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
        "https://tauka.io,https://www.tauka.io,"
        "https://www.tauka.com,https://tauka.com"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ── Deployment sanity ─────────────────────────────────────────────────────

    @staticmethod
    def _is_local(value: str) -> bool:
        v = (value or "").lower()
        return any(h in v for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))

    @property
    def mail_leaves_this_machine(self) -> bool:
        """
        Will a send actually reach a stranger's inbox?

        `console` writes to disk. `smtp` against a local host is a catcher
        (Mailpit), which is also local-only. Anything else — Resend, or SMTP
        against a real host like smtp.zoho.com — delivers for real.
        """
        if self.email_provider == "console":
            return False
        if self.email_provider == "smtp":
            return not self._is_local(self.smtp_host)
        return True

    def deployment_problems(self) -> list[str]:
        """
        Misconfigurations that are silent at startup and only visible to a
        recipient, checked once when the app boots.

        The specific failure this exists to catch: the dev `.env` points
        WEB_BASE_URL at the Vite dev server so links in locally-caught mail are
        clickable, which is correct locally — but deploying that same file sends
        real Zoho mail to real people containing `http://localhost:5173/...`.
        Nothing raises, nothing logs, and the send is reported as `sent`. The
        recipient sees a dead link.

        Inferred rather than gated on an ENVIRONMENT var deliberately: a var
        that has to be set on the host is one more thing a deploy can forget,
        and forgetting it would silence the check exactly when it matters. A
        real mail provider paired with a localhost URL is a contradiction in any
        environment, so it needs no flag to detect.
        """
        problems: list[str] = []
        if not self.mail_leaves_this_machine:
            return problems

        for name, value in (("WEB_BASE_URL", self.web_base_url),
                            ("API_BASE_URL", self.api_base_url)):
            if self._is_local(value):
                problems.append(
                    f"{name}={value!r} is a local address, but EMAIL_PROVIDER="
                    f"{self.email_provider!r} delivers mail externally. Every "
                    f"link built from {name} will be dead for the recipient. "
                    f"Unset it to use the default, or set it to the public origin."
                )
        return problems

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
