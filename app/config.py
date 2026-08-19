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
    #   ENGINE_FREE_QUOTAS=cb_vinu=12000,cb_paluku=100000
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
            "https://chatbucket.business,"
            # Both apex and www: they are two different origins to a
            # browser, and whichever is missing is the one someone opens.
            "https://www.chatbucket.business"
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
    # Log the full SMTP conversation, including the provider's queue id for
    # each accepted message. For diagnosing "we sent it but nothing arrived",
    # which is otherwise unfalsifiable. Off in production: the dialogue
    # contains the base64-encoded AUTH credentials.
    smtp_debug: bool = Field(default=False)

    # Must be an address on the domain the SMTP account actually authenticates
    # as, or DMARC rejects the mail. The previous default here was
    # `no-reply@chatbucket.chat`, and that domain publishes
    # `v=DMARC1; p=reject` with an SPF authorising a different provider than
    # the one we send through — so a deploy that forgot to override this did
    # not merely land in spam, it got refused at the door.
    email_from: str = Field(default="support@chatbucket.business")
    email_from_name: str = Field(default="ChatBucket")
    # Where demo requests are sent. Empty disables that notification.
    sales_email: str = Field(default="")
    # Printed in every template's help block, and the Reply-To on mail a
    # customer might answer. Not the same as `EMAIL_FROM`, which is a no-reply.
    support_email: str = Field(default="support@chatbucket.business")
    # Base URL of the site that hosts the password-reset page, used to build
    # the link in the reset email.
    app_base_url: str = Field(default="http://localhost:3000")
    password_reset_path: str = Field(default="/reset-password")

    # --- Links inside the designed emails ----------------------------------
    # Every template carries the same header/footer furniture: a "Visit
    # ChatBucket" button, "Track Query Status", the dashboard CTA and the
    # policy links. They are settings rather than constants because the
    # marketing site and the dashboard are different hosts, and staging is a
    # third. `emailtemplates.base_context` feeds these to every render.
    marketing_url: str = Field(default="https://chatbucket.business")
    # Everything is stored in UTC. Dates and times *shown to a customer* are
    # rendered in this zone, because "31 July, 6:59 PM" on a receipt has to
    # match the clock the customer paid by, not the server's.
    display_timezone: str = Field(default="Asia/Kolkata")
    dashboard_path: str = Field(default="/dashboard")
    login_path: str = Field(default="/login")
    # Where a customer follows up a support request. Relative to the app, or an
    # absolute URL if the help desk lives elsewhere.
    track_query_path: str = Field(default="/support/tickets")
    privacy_policy_path: str = Field(default="/privacy-policy")
    terms_path: str = Field(default="/terms-of-service")

    def _app_url(self, path: str) -> str:
        """Resolve a configured path against the app, passing absolutes through."""
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.app_base_url.rstrip('/')}/{path.lstrip('/')}"

    @property
    def dashboard_url_for(self) -> str:
        return self._app_url(self.dashboard_path)

    @property
    def login_url_for(self) -> str:
        return self._app_url(self.login_path)

    @property
    def track_query_url_for(self) -> str:
        return self._app_url(self.track_query_path)

    @property
    def privacy_policy_url_for(self) -> str:
        # Policy pages live on the marketing site, not the dashboard.
        if self.privacy_policy_path.startswith(("http://", "https://")):
            return self.privacy_policy_path
        return f"{self.marketing_url.rstrip('/')}/{self.privacy_policy_path.lstrip('/')}"

    @property
    def terms_url_for(self) -> str:
        if self.terms_path.startswith(("http://", "https://")):
            return self.terms_path
        return f"{self.marketing_url.rstrip('/')}/{self.terms_path.lstrip('/')}"

    # --- Email verification -------------------------------------------------
    # When true, an unverified account cannot create API keys. Off by default:
    # turning it on locks out every existing account until they verify.
    require_email_verification: bool = Field(default=False)
    verification_token_expire_hours: int = Field(default=48)
    email_verify_path: str = Field(default="/verify-email")
    # The verification email carries a 6-digit code as well as the link. The
    # code is what someone types on a phone; it is short-lived because a
    # six-digit secret is only strong while the window to guess it is small.
    email_otp_expire_minutes: int = Field(default=10, ge=1, le=60)

    # --- SMS / phone verification -------------------------------------------
    # An Indian number verifies by SMS and skips the email code entirely; every
    # other country verifies by email as before. `verification.channel_for`
    # is the single place that decides, so nothing else has to know the rule.
    #
    # Vendor-neutral on purpose, exactly like the SMTP seam: a gateway is a
    # config change, not a code change. Backends mirror EMAIL_BACKEND —
    # `http` really sends, `console`/`memory` do not, `disabled` drops.
    sms_backend: str = Field(
        default="auto",
        description=(
            "auto (http when SMS_API_URL is set, else console) | http | console "
            "| memory (tests) | disabled."
        ),
    )
    sms_api_url: str = Field(default="")
    sms_username: str = Field(default="")
    sms_api_key: str = Field(default="")
    # Numbers are stored E.164 (`+9199…`); this gateway wants bare local
    # digits. A property of the gateway, not the number, so it is a setting.
    sms_strip_country_code: bool = Field(default=True)
    # The DLT-registered sender header and template id. India requires both to
    # be pre-registered; an unregistered pair is rejected by the operator, not
    # by the gateway, so a wrong value here looks like silent non-delivery.
    sms_sender_id: str = Field(default="")
    sms_template_id: str = Field(default="")
    # The registered OTP template ends with a second variable. Its value is a
    # property of that registration, not of this service, so it is configured
    # rather than guessed. Empty is allowed; `render_otp_message` strips the
    # trailing space so the delivered text stays clean.
    sms_template_suffix: str = Field(default="")
    sms_timeout_seconds: int = Field(default=15)
    # Dial codes that verify by SMS. Anything else falls back to email.
    sms_country_codes: str = Field(default="+91")
    phone_otp_expire_minutes: int = Field(default=10, ge=1, le=60)
    phone_otp_max_attempts: int = Field(default=5, ge=1, le=20)
    # How long a number proven on the signup form stays usable for creating the
    # account. Long enough to finish typing the rest of the form, short enough
    # that the proof still means "somebody held that handset just now". Bounding
    # it is what stops a verified number being attached to an account created
    # hours later by somebody else.
    phone_verification_grace_minutes: int = Field(default=30, ge=1, le=1440)

    @property
    def sms_country_code_list(self) -> list[str]:
        return [c.strip() for c in self.sms_country_codes.split(",") if c.strip()]

    @property
    def resolved_sms_backend(self) -> str:
        """The backend actually in use, with ``auto`` decided."""
        backend = self.sms_backend.lower().strip()
        if backend != "auto":
            return backend
        return "http" if self.sms_api_url.strip() else "console"
    # Wrong codes tolerated before the code is burned. Without a cap, six
    # digits is a million tries against a static value.
    email_otp_max_attempts: int = Field(default=5, ge=1, le=20)

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
    # 1 credit = ₹1, so this is ₹100 of free usage for every new account.
    # It is a real cost per signup, which is why the welcome email states
    # the amount and `FREE_CREDIT_VALIDITY_DAYS` sets the window it is
    # presented with. Set to 0 to require a top-up before the first call.
    signup_bonus_credits: str = Field(default="100")
    # Shared secret the payment gateway's webhook must present to mark a
    # top-up paid. Unset means no request can ever grant credits.
    billing_webhook_secret: str = Field(default="")

    # --- Lifecycle notifications -------------------------------------------
    # How long the signup bonus is presented as being good for. Nothing expires
    # credits automatically — the product line is that credits never expire —
    # so this is the window the welcome and reminder emails talk about, and the
    # figure the reminder counts down to.
    free_credit_validity_days: int = Field(default=30, ge=1)
    # Send the "credits expire soon" reminder once the remaining validity drops
    # to this many days.
    free_credit_reminder_days: int = Field(default=7, ge=1)
    # Nudge accounts that registered this many days ago and have never metered
    # a single call. Sent once per account.
    onboarding_nudge_after_days: int = Field(default=3, ge=1)
    # Chase accounts that registered this many hours ago and never confirmed
    # their address. With REQUIRE_EMAIL_VERIFICATION on they are stuck: signed
    # up, credited, and unable to create a key. A *fresh* code is minted, since
    # the one from signup expired within minutes. Sent once per account.
    verification_reminder_after_hours: int = Field(default=24, ge=1)
    # Ceiling on one broadcast (announcement / maintenance). A run that would
    # exceed it stops and says so, rather than quietly mailing half the base.
    broadcast_max_recipients: int = Field(default=5000, ge=1)
    # Messages in flight at once during a broadcast or report run. SMTP
    # providers rate-limit, and one connection per recipient in parallel is the
    # fastest way to get a sending domain throttled.
    broadcast_concurrency: int = Field(default=5, ge=1, le=50)

    # --- Notification scheduler ---------------------------------------------
    # Runs the monthly report, the free-credit reminder and the onboarding
    # nudge on a timer inside the app, so they need no external cron.
    #
    # **Off by default, and that is deliberate.** Turning it on starts mailing
    # real customers unattended. A deployment should opt in only once its
    # sending domain is authenticated (SPF + DKIM + DMARC) and a test broadcast
    # has been seen to reach an inbox rather than a spam folder.
    notification_scheduler_enabled: bool = Field(default=False)
    # How often the loop wakes to see whether anything is due. Not how often
    # jobs run — each job runs once per its own period, whatever this is.
    notification_scheduler_interval_seconds: int = Field(default=900, ge=60)
    # Hour of the day, in DISPLAY_TIMEZONE, that the daily jobs run. Mail sent
    # at 3am reads as machine-generated and is opened less.
    notification_scheduler_hour: int = Field(default=6, ge=0, le=23)
    # Day of the month the monthly report goes out, covering the month before.
    notification_monthly_report_day: int = Field(default=1, ge=1, le=28)

    # --- Payment gateway ----------------------------------------------------
    # These field names are also the environment-variable names, and they are
    # set on the running apps, so they keep the gateway's name where renaming
    # them would mean a coordinated config change for no behavioural gain.
    # Order creation authenticates with key id + secret (HTTP Basic).
    razorpay_key_id: str = Field(default="")
    razorpay_key_secret: str = Field(default="")
    # A SEPARATE value, set in the gateway's dashboard when registering the
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
    # Nothing configured logging before, so the root logger sat at WARNING and
    # every `logger.info` in this app was dropped: the email backend named at
    # boot, "sent <subject> to <address>", the scheduler's state, each job's
    # result. All the lines the docs tell an operator to grep for existed only
    # in development. DEBUG is very noisy — it includes per-request detail.
    log_level: str = Field(default="INFO")

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
    def cors_origin_regex(self) -> str | None:
        """Any localhost port, in development only. None in production.

        An origin is matched exactly, so a dev server that came up on a port
        nobody listed is a failed preflight with no message on the server side
        — and `next dev` quietly picks 3001 when 3000 is busy, which is the
        usual way this bites. Allowing any loopback port in development removes
        the whole class of it.

        Deliberately gated on `is_dev`: this pairs with `allow_credentials`, so
        in production the exact list is the only thing standing between a
        customer's session and any site that asks for it.
        """
        if not self.is_dev:
            return None
        return r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"

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
