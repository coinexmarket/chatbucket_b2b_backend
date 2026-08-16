"""Request rate limiting.

Counters live in Mongo rather than in process memory, because the app runs
behind more than one worker: an in-process counter would give a caller N times
the intended limit on an N-worker deployment, which is the same as having no
limit at all. A fixed window per (scope, identifier) is incremented with a
single atomic `$inc`, so concurrent requests cannot both read "4 of 5" and both
proceed. Expired windows are removed by a TTL index rather than by any sweep of
ours.

Redis would be the usual home for this and would be cheaper per hit; Mongo is
used because it is already here, and a limiter that exists beats one that waits
for new infrastructure. `hit()` is the only thing that would need replacing.

**Fails open.** If the counter store is unreachable the request is allowed, and
the failure logged. A limiter outage should not take sign-in down with it —
the limiter protects against abuse, it is not the authorisation check.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .config import get_settings
from .database import rate_limits_collection

logger = logging.getLogger("chatbucket_b2b.ratelimit")


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: int


# Public, unauthenticated endpoints. Login is limited per IP *and* per email:
# per-IP alone lets a botnet spread an attack on one account across many
# addresses, and per-email alone lets one address work through many accounts.
#
# Per-IP limits are deliberately loose and per-account limits tight. A whole
# office shares one address behind NAT, so a strict per-IP cap locks out
# colleagues of whoever is being attacked; the per-account limit does the real
# security work, with per-IP acting only as a coarse flood guard.
LIMITS: dict[str, Limit] = {
    "login_ip": Limit(50, 900),          # coarse flood guard, NAT-friendly
    "login_email": Limit(5, 900),        # the real brute-force defence
    "register_ip": Limit(10, 3600),
    "forgot_ip": Limit(20, 3600),
    "forgot_email": Limit(3, 3600),      # also caps reset emails per address
    "demo_ip": Limit(10, 3600),
    "contest_ip": Limit(10, 3600),
    # The six-digit verification code. The per-address attempt counter on the
    # user document is the real defence — it burns the code after a handful of
    # wrong guesses — but that counter resets with every resend, so this caps
    # how fast the resend-and-guess cycle can be repeated.
    "verify_otp_ip": Limit(30, 900),
    "verify_otp_email": Limit(10, 900),
}


@dataclass(frozen=True)
class Result:
    allowed: bool
    remaining: int
    retry_after: int


def client_ip(request: Request) -> str:
    """The caller's address.

    `X-Forwarded-For` is only believed when `TRUST_PROXY_HEADERS` is on. The
    header is trivially forged, so trusting it by default would let anyone
    bypass every per-IP limit by inventing an address per request. Turn it on
    only when a proxy you control rewrites it.
    """
    settings = get_settings()
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def hit(scope: str, identifier: str, limit: Limit) -> Result:
    """Count one request against a window. Never raises."""
    now = int(time.time())
    window_start = now - (now % limit.window_seconds)
    reset_at = window_start + limit.window_seconds
    key = f"{scope}:{identifier}:{window_start}"

    try:
        doc = await rate_limits_collection().find_one_and_update(
            {"_id": key},
            {
                "$inc": {"count": 1},
                # Only on insert: the expiry belongs to the window, and
                # refreshing it on every hit would keep a hot key alive forever.
                "$setOnInsert": {"expires_at": _expiry(reset_at)},
            },
            upsert=True,
            return_document=True,
        )
        count = int(doc.get("count", 1))
    except Exception as exc:
        logger.error("rate limit store unavailable, allowing %s/%s: %s", scope, identifier, exc)
        return Result(True, limit.requests, 0)

    return Result(
        allowed=count <= limit.requests,
        remaining=max(limit.requests - count, 0),
        retry_after=max(reset_at - now, 1),
    )


def _expiry(reset_at: int):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(reset_at, tz=timezone.utc)


async def enforce(scope: str, identifier: str, limit_name: str | None = None) -> None:
    """Count a request and raise 429 when the window is exhausted."""
    if not get_settings().rate_limit_enabled:
        return
    limit = LIMITS[limit_name or scope]
    result = await hit(scope, identifier, limit)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again shortly.",
            headers={"Retry-After": str(result.retry_after)},
        )


async def enforce_limit(scope: str, identifier: str, limit: Limit) -> Result:
    """Count a request against a caller-supplied limit, raising 429 if spent.

    For limits that aren't fixed in `LIMITS` — plan tiers, where the ceiling
    depends on which plan the customer is on. Returns the result so the caller
    can surface `X-RateLimit-*` headers on the success path too.
    """
    if not get_settings().rate_limit_enabled:
        return Result(True, limit.requests, 0)
    result = await hit(scope, identifier, limit)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded: {limit.requests} requests per "
                f"{limit.window_seconds}s on your plan."
            ),
            headers={
                "Retry-After": str(result.retry_after),
                "X-RateLimit-Limit": str(limit.requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(result.retry_after),
            },
        )
    return result


def by_ip(limit_name: str):
    """FastAPI dependency limiting an endpoint by caller address."""

    async def dependency(request: Request) -> None:
        await enforce(limit_name, client_ip(request), limit_name)

    return dependency
