"""Security primitives: password hashing, JWT tokens, and API keys.

Password hashing uses PBKDF2-HMAC-SHA256 from the standard library — strong,
salted, and with no native/Rust build dependency (important on very new Python
versions). Tokens use PyJWT (pure Python).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import jwt

from .config import get_settings

# --- Password hashing (PBKDF2-HMAC-SHA256) --------------------------------
_PBKDF2_ITERATIONS = 240_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify a password against a stored PBKDF2 string."""
    try:
        algorithm, iter_s, salt_hex, hash_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """A throwaway hash carrying the same work factor as a real one.

    Verifying against this when no user matches keeps login's response time the
    same whether or not the email exists. Otherwise the miss path skips ~240k
    PBKDF2 rounds and returns visibly faster, which turns the endpoint into an
    account-enumeration oracle. Built once, from a random password nothing can
    match.
    """
    return hash_password(secrets.token_urlsafe(32))


# --- JWT access tokens -----------------------------------------------------

def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "type": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        return jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return None


def token_version_of(user: dict) -> int:
    """The user's current token generation.

    Bumping this on the user document invalidates every token issued before
    it. Users created before the field existed read as 0, which matches the
    claim default in `create_access_token_for_user`, so their live tokens keep
    working until something actually revokes them.
    """
    return user.get("token_version", 0)


def create_access_token_for_user(user: dict) -> str:
    """Issue an access token carrying the user's identity and token version.

    The one place the claim shape is defined, so `deps.get_current_user` can
    rely on it without the two drifting apart.
    """
    return create_access_token(
        str(user["_id"]),
        extra={"email": user["email"], "ver": token_version_of(user)},
    )


# --- API keys --------------------------------------------------------------
# Format: cb_live_<random>. Only the SHA-256 hash is stored; the plaintext is
# shown to the user exactly once at creation time.
_API_KEY_PREFIX = "cb_live_"


def generate_api_key() -> tuple[str, str, str, str]:
    """Return ``(full_key, prefix, sha256_hash, last4)``."""
    raw = secrets.token_urlsafe(32)
    full = f"{_API_KEY_PREFIX}{raw}"
    return full, _API_KEY_PREFIX.rstrip("_"), hash_api_key(full), full[-4:]


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


# --- Opaque reset tokens ---------------------------------------------------

def generate_reset_token() -> tuple[str, str]:
    """Return ``(token, sha256_hash)`` for a password-reset link."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
