"""Security primitives: password hashing, JWT tokens, and API keys.

Password hashing uses PBKDF2-HMAC-SHA256 from the standard library — strong,
salted, and with no native/Rust build dependency (important on very new Python
versions). Tokens use PyJWT (pure Python).

The hashing work factor is deliberately expensive (~200ms), so request handlers
must use the ``*_async`` wrappers below rather than the sync functions — see
their docstring.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import jwt
from starlette.concurrency import run_in_threadpool

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


# --- Async wrappers (use these from request handlers) ----------------------
# PBKDF2 at 240k iterations costs ~200ms of CPU. Called directly from an async
# handler it blocks the event loop for that whole time, stalling every other
# in-flight request — and since `login` runs the hash even when no user matches
# (see `dummy_password_hash`), unauthenticated traffic alone could saturate the
# worker. Running it in the threadpool keeps the loop free; `hashlib` releases
# the GIL during PBKDF2, so the work genuinely runs in parallel.


async def hash_password_async(password: str) -> str:
    """`hash_password`, off the event loop."""
    return await run_in_threadpool(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    """`verify_password`, off the event loop."""
    return await run_in_threadpool(verify_password, password, stored)


def warm_password_hasher() -> None:
    """Populate the `dummy_password_hash` cache before serving traffic.

    It is `lru_cache`d, so otherwise the very first login with an unknown email
    pays the ~200ms hash *on the event loop* to build it. Called from the app
    lifespan, where blocking costs nothing.
    """
    dummy_password_hash()


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


def generate_refresh_token() -> tuple[str, str]:
    """Return ``(token, sha256_hash)`` for a session refresh token.

    Opaque and random rather than a JWT: a refresh token must be revocable the
    instant a user signs out, and that means checking it against storage on
    every use — which a self-contained JWT is specifically designed to avoid.
    Only the hash is stored, so a database leak does not hand over live
    sessions.
    """
    token = secrets.token_urlsafe(48)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_verification_token() -> tuple[str, str]:
    """Return ``(token, sha256_hash)`` for an email-verification link."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_email_otp(code: str) -> str:
    """Keyed hash of a verification code, for storage.

    HMAC under `JWT_SECRET` rather than a bare SHA-256: there are only a
    million six-digit codes, so a plain digest is reversed by a laptop in
    seconds and storing one would be the same as storing the code. The key
    lives in the environment, not the database, so a dump of the users
    collection on its own reveals nothing.
    """
    secret = get_settings().jwt_secret.encode("utf-8")
    return hmac.new(secret, code.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_email_otp() -> tuple[str, str]:
    """Return ``(code, keyed_hash)`` for the 6-digit email verification code.

    `secrets.randbelow`, not `random`: this is a credential, and the stdlib's
    default generator is seeded predictably enough to guess. Zero-padded, so
    "004821" is a valid code and the keyspace is the full million rather than
    the 900,000 you get by starting at 100000.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    return code, hash_email_otp(code)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
