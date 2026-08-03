"""Refresh tokens: keeping a session alive without a long-lived credential.

Access tokens last 24h and are self-contained, which makes them fast to check
but impossible to revoke individually. Refresh tokens are the opposite —
opaque, stored (hashed), and checked against the database on every use — so a
sign-out takes effect immediately.

**Rotation with reuse detection.** Each refresh consumes the token and issues a
new one. A token therefore has exactly one legitimate use; seeing it a second
time means the value leaked and two parties now hold it. That cannot be told
apart from the attacker refreshing first, so the whole family is revoked and
everyone is signed out. Losing a session is a far better outcome than silently
sharing it with whoever copied the token.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .database import refresh_tokens_collection
from .security import generate_refresh_token, hash_token

logger = logging.getLogger("chatbucket_b2b.sessions")


async def issue(user_id, family: str | None = None) -> tuple[str, datetime]:
    """Create a refresh token. Returns ``(token, expires_at)``.

    ``family`` chains a rotated token to its predecessor, so a reuse can revoke
    every descendant of the original sign-in rather than just one row.
    """
    settings = get_settings()
    token, token_hash = generate_refresh_token()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.refresh_token_expire_days)

    document = {
        "user_id": user_id,
        "token_hash": token_hash,
        "family": family or token_hash,
        "revoked": False,
        "used_at": None,
        "created_at": now,
        "expires_at": expires_at,
    }
    await refresh_tokens_collection().insert_one(document)
    return token, expires_at


async def revoke_family(family: str, reason: str) -> int:
    """Revoke every token descended from one sign-in."""
    result = await refresh_tokens_collection().update_many(
        {"family": family, "revoked": False}, {"$set": {"revoked": True, "reason": reason}}
    )
    return result.modified_count


async def consume(token: str) -> dict | None:
    """Spend a refresh token, returning its record, or None if unusable.

    The token is claimed with a conditional update, so two concurrent refreshes
    cannot both succeed — exactly one gets the rotation.
    """
    token_hash = hash_token(token)

    claimed = await refresh_tokens_collection().find_one_and_update(
        {"token_hash": token_hash, "revoked": False, "used_at": None},
        {"$set": {"used_at": datetime.now(timezone.utc)}},
        return_document=True,
    )
    if claimed is not None:
        expires = claimed.get("expires_at")
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is not None and expires < datetime.now(timezone.utc):
            return None
        return claimed

    # Nothing claimable. If the token exists but was already spent, it has been
    # replayed — treat the whole family as compromised.
    existing = await refresh_tokens_collection().find_one({"token_hash": token_hash})
    if existing is not None and existing.get("used_at") is not None:
        revoked = await revoke_family(existing["family"], "refresh token reused")
        logger.warning(
            "refresh token replayed for user %s; revoked %d session(s)",
            existing.get("user_id"),
            revoked,
        )
    return None


async def revoke_one(token: str) -> bool:
    """Sign out a single session (the caller's own logout)."""
    result = await refresh_tokens_collection().update_one(
        {"token_hash": hash_token(token), "revoked": False},
        {"$set": {"revoked": True, "reason": "logout"}},
    )
    return result.modified_count > 0


async def revoke_all_for_user(user_id, reason: str = "logout_all") -> int:
    """Sign out every session for a user."""
    result = await refresh_tokens_collection().update_many(
        {"user_id": user_id, "revoked": False},
        {"$set": {"revoked": True, "reason": reason}},
    )
    return result.modified_count


async def active_count(user_id) -> int:
    return await refresh_tokens_collection().count_documents(
        {"user_id": user_id, "revoked": False, "used_at": None}
    )
