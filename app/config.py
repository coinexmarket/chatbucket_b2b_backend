"""Application configuration, loaded from environment variables.

All runtime configuration lives here so the rest of the app never reads
``os.environ`` directly. Values are read lazily from a ``.env`` file (for local
development) or the process environment (in production / Docker).
"""
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The shipped placeholder in .env.example. Safe for local work, never for prod.
DEFAULT_JWT_SECRET = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
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

    # --- HTTP / CORS -------------------------------------------------------
    cors_origins: str = Field(
        default=(
            "http://localhost:3000,"
            "https://chatbucket.chat,"
            "https://www.chatbucket.chat,"
            "https://chatbucket.business"
        ),
        description="Comma-separated list of allowed CORS origins.",
    )

    app_name: str = "ChatBucket B2B Backend"
    environment: str = Field(default="development")

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
        if self.is_dev:
            return self

        if self.jwt_secret == DEFAULT_JWT_SECRET:
            raise ValueError(
                f"JWT_SECRET is still the default placeholder while "
                f"ENVIRONMENT={self.environment!r}. Tokens would be forgeable by "
                "anyone with this repo. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
