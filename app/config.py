"""Application configuration, loaded from environment variables.

All runtime configuration lives here so the rest of the app never reads
``os.environ`` directly. Values are read lazily from a ``.env`` file (for local
development) or the process environment (in production / Docker).
"""
import os
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The shipped placeholder in .env.example. Safe for local work, never for prod.
DEFAULT_JWT_SECRET = "dev-insecure-change-me"

_EMAIL_BACKENDS = {"auto", "smtp", "console", "memory", "disabled"}

# The smoke suites set this before importing the app, so that a developer's
# local `.env` cannot change what they assert. Without it the suite tests
# whichever machine it runs on: `SIGNUP_BONUS_CREDITS=1000` in one developer's
# `.env` silently breaks twelve credit assertions that pass in CI, where no
# `.env` exists. Config is the one module allowed to read `os.environ`.
IGNORE_DOTENV_VAR = "CHATBUCKET_IGNORE_DOTENV"
_ENV_FILE = None if os.environ.get(IGNORE_DOTENV_VAR) == "1" else ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # --- MongoDB -----------------------------------------------------------
    # A single connection string powers the logical databases below. Keeping
    # the database *names* configurable lets us physically separate concerns
    # (they can live on different clusters by changing MONGODB_URI).
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB connection string (required at runtime).",
    )
    b2b_db_name: str = Field(
        default="chatbucket_b2b",
        description="Database for accounts, API keys and usage records.",
    )
    blog_db_name: str = Field(
        default="chatbucket",
        description="Database holding blogs, categories and subscriptions.",
    )
    contest_db_name: str = Field(
        default="ChatBucketHackathon",
        description="Database holding contest registrations (kept separate).",
    )

    # --- Auth / JWT --------------------------------------------------------
    jwt_secret: str = Field(
        default=DEFAULT_JWT_SECRET,
        description="HMAC secret for signing JWT access tokens.",
    )
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=60 * 24)  # 24h
    reset_token_expire_minutes: int = Field(default=30)

    # Currency label for billing amounts (all rates are in Indian Rupees).
    currency: str = Field(default="INR")

    # --- Rate limiting / sessions -----------------------------------------
    rate_limit_enabled: bool = Field(default=True)
    # Enforce the per-minute request limits each plan is sold with. Costs one
    # extra Mongo write per metered call; turn off to advertise the limits
    # without applying them (`GET /limits` reports which is in effect).
    enforce_plan_rate_limits: bool = Field(default=True)
    # Believe `X-Forwarded-For` when resolving the caller's address. Only turn
    # this on behind a proxy you control: the header is trivially forged, and
    # trusting it otherwise lets anyone bypass every per-IP limit.
    trust_proxy_headers: bool = Field(default=False)
    # Refresh tokens outlive the 24h access token so a session survives
    # without keeping a long-lived credential in the browser.
    refresh_token_expire_days: int = Field(default=30)

    # --- Service status ----------------------------------------------------
    # Shared secret for heartbeats and manual status changes. Unset means
    # nothing can write a status — anyone who could set "operational" could
    # hide a real outage from every customer at once.
    status_webhook_secret: str = Field(default="")
    # A heartbeat or probe older than this reads `unknown` rather than staying
    # green. Manual statuses are exempt.
    status_stale_after_seconds: int = Field(default=300)
    # Optional health URLs to poll, as `key=url` pairs:
    #   STATUS_PROBE_URLS=tts=https://tts.internal/health,stt=https://stt.internal/health
    # Empty disables probing entirely.
    status_probe_urls: str = Field(default="")
    status_probe_interval_seconds: int = Field(default=60)

    @property
    def status_probe_map(self) -> dict[str, str]:
        probes: dict[str, str] = {}
        for entry in self.status_probe_urls.split(","):
            key, _, url = entry.partition("=")
            if key.strip() and url.strip():
                probes[key.strip().lower()] = url.strip()
        return probes

    # --- Engine capacity ---------------------------------------------------
    # Shared secret for the engine-burn view. That view reports what our own
    # capacity costs us and how much allowance is left, which is commercial
    # information about us, not about the customer — so it is gated by an
    # operator secret rather than a user session. Unset means it cannot be read.
    ops_secret: str = Field(default="")
    # Allowances, as `engine=amount` pairs in the engine's own unit:
    #   ENGINE_FREE_QUOTAS=cb_vinu=12000,cb_palukulu=100000
    # Ships empty: the size of an allowance is a fact about an arrangement this
    # service cannot observe, and a guessed "remaining" would be read as
    # authoritative. Unset means consumption is counted but `remaining` is null.
    engine_free_quotas: str = Field(default="")

    @property
    def engine_quota_map(self) -> dict[str, float]:
        quotas: dict[str, float] = {}
        for entry in self.engine_free_quotas.split(","):
            key, _, amount = entry.partition("=")
            if not key.strip() or not amount.strip():
                continue
            try:
                quotas[key.strip().lower()] = float(amount)
            except ValueError:
                # A malformed pair is skipped rather than crashing startup: the
                # quota is a display figure, and refusing to boot over a typo
                # in it would take the whole API down for a cosmetic setting.
                continue
        return quotas

    # --- HTTP / CORS -------------------------------------------------------
    cors_origins: str = Field(
        default=(
            # Local dev servers. An origin is matched exactly — scheme, host and
            # port all count — so `localhost` and `127.0.0.1` are two different
            # origins to a browser however identical they look here, and both
            # are listed rather than leaving whichever one someone opens to
            # fail a preflight with no obvious cause.
            "http://localhost:3000,"
            "http://127.0.0.1:3000,"
            "http://localhost:4100,"
            "http://127.0.0.1:4100,"
            "https://chatbucket.chat,"
            "https://www.chatbucket.chat,"
            "https://chatbucket.business"
        ),
        description="Comma-separated list of allowed CORS origins.",
    )

    # --- Email -------------------------------------------------------------
    # SMTP rather than a vendor HTTP API on purpose: SendGrid, SES, Mailgun,
    # Postmark and Gmail all speak it, so the provider is a config change
    # instead of a code change, and it needs no extra dependency.
    email_backend: str = Field(
        default="auto",
        description=(
            "auto (smtp when SMTP_HOST is set, else console) | smtp | console "
            "| memory (tests) | disabled."
        ),
    )
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    # Port 465 is implicit TLS; 587 is plaintext-then-STARTTLS. Defaults suit
    # 587, which is what most providers document.
    smtp_use_ssl: bool = Field(default=False)
    smtp_starttls: bool = Field(default=True)
    smtp_timeout_seconds: int = Field(default=15)

    email_from: str = Field(default="no-reply@chatbucket.chat")
    email_from_name: str = Field(default="ChatBucket")
    # Where demo requests are sent. Empty disables that notification.
    sales_email: str = Field(default="")
    # Base URL of the site that hosts the password-reset page, used to build
    # the link in the reset email.
    app_base_url: str = Field(default="http://localhost:3000")
    password_reset_path: str = Field(default="/reset-password")

    # --- Email verification -------------------------------------------------
    # When true, an unverified account cannot create API keys. Off by default:
    # turning it on locks out every existing account until they verify.
    require_email_verification: bool = Field(default=False)
    verification_token_expire_hours: int = Field(default=48)
    email_verify_path: str = Field(default="/verify-email")

    @property
    def email_verify_url_for(self) -> str:
        return f"{self.app_base_url.rstrip('/')}{self.email_verify_path}"

    # Recorded on each account at signup, so a later change to the terms can
    # be told apart from what a given customer actually agreed to.
    terms_version: str = Field(default="v1")

    # --- Credits / billing -------------------------------------------------
    # When true, `POST /usage` debits the customer's credit balance and refuses
    # with 402 once it is exhausted. Turning it off meters usage without ever
    # blocking, which is how the service behaved before billing existed.
    enforce_credit_balance: bool = Field(default=True)
    # Credits granted to a brand-new account. 0 means a new customer must top
    # up before any metered call succeeds.
    signup_bonus_credits: str = Field(default="0")
    # Shared secret the payment gateway's webhook must present to mark a
    # top-up paid. Unset means no request can ever grant credits.
    billing_webhook_secret: str = Field(default="")

    # --- Razorpay ----------------------------------------------------------
    # Order creation authenticates with key id + secret (HTTP Basic).
    razorpay_key_id: str = Field(default="")
    razorpay_key_secret: str = Field(default="")
    # A SEPARATE value, set in the Razorpay dashboard when registering the
    # webhook. Webhook payloads are signed with this, not with the key secret;
    # using the wrong one silently rejects every callback.
    razorpay_webhook_secret: str = Field(default="")
    razorpay_timeout_seconds: int = Field(default=15)

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    # Invoice numbering, e.g. INV-0001. The sequence is global and gap-free;
    # change the prefix only between accounting periods, never mid-sequence.
    invoice_number_prefix: str = Field(default="INV-")
    invoice_number_padding: int = Field(default=4, ge=1, le=12)

    app_name: str = "ChatBucket B2B Backend"
    environment: str = Field(default="development")

    @property
    def resolved_email_backend(self) -> str:
        """The backend actually in use, with ``auto`` decided."""
        backend = self.email_backend.lower().strip()
        if backend != "auto":
            return backend
        return "smtp" if self.smtp_host.strip() else "console"

    @property
    def password_reset_url_for(self) -> str:
        return f"{self.app_base_url.rstrip('/')}{self.password_reset_path}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.environment.lower() in {"dev", "development", "local"}

    @model_validator(mode="after")
    def _reject_insecure_production_config(self) -> "Settings":
        """Refuse to start a non-development process with the dev defaults.

        Both `JWT_SECRET` and `ENVIRONMENT` default to development-safe values,
        so an env var that never made it into the deploy would otherwise boot
        silently: tokens signed with a secret that is published in this repo,
        and `/auth/forgot-password` handing the plaintext reset token to any
        caller who knows an email address. Fail closed instead.
        """
        backend = self.email_backend.lower().strip()
        if backend not in _EMAIL_BACKENDS:
            raise ValueError(
                f"EMAIL_BACKEND={self.email_backend!r} is not one of "
                f"{', '.join(sorted(_EMAIL_BACKENDS))}."
            )
        if backend == "smtp" and not self.smtp_host.strip():
            raise ValueError("EMAIL_BACKEND=smtp requires SMTP_HOST to be set.")

        if self.is_dev:
            return self

        if self.jwt_secret == DEFAULT_JWT_SECRET:
            raise ValueError(
                f"JWT_SECRET is still the default placeholder while "
                f"ENVIRONMENT={self.environment!r}. Tokens would be forgeable by "
                "anyone with this repo. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

        # A deploy that forgot SMTP_HOST would otherwise fall back to printing
        # reset links to the log: password reset would appear to work and
        # silently deliver nothing. Fail closed on the *accident*, while still
        # allowing a deliberate EMAIL_BACKEND=disabled.
        if backend == "auto" and self.resolved_email_backend != "smtp":
            raise ValueError(
                f"No SMTP_HOST is configured while ENVIRONMENT="
                f"{self.environment!r}, so password-reset emails would only be "
                "written to the log and never delivered. Set SMTP_HOST (and "
                "credentials), or set EMAIL_BACKEND=disabled to acknowledge "
                "that this deployment sends no email."
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
