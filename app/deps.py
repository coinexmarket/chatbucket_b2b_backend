"""Authentication dependencies.

Two ways to authenticate:

* ``get_current_user`` — a Bearer JWT, used by the dashboard/user-facing
  endpoints (profile, API-key management, usage history).
* ``get_api_user`` — an ``X-API-Key`` header, used for machine-to-machine
  calls (reporting usage from the STT/TTS/translation/voice services).

Both resolve to the same user document (with ``_id`` as a string).
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import api_keys_collection, users_collection
from .security import decode_access_token, hash_api_key, token_version_of

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Resolve the user from a Bearer JWT access token."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await _load_user(payload.get("sub"))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )

    # A password reset or change bumps `token_version`, which retires every
    # token issued before it. Without this, a token stolen before the reset
    # stays valid for the rest of its lifetime (up to 24h) — i.e. the reset
    # would not actually lock the attacker out.
    if payload.get("ver", 0) != token_version_of(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is no longer valid. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_api_user(x_api_key: str | None = Header(default=None)) -> dict:
    """Resolve the user (and record last-use) from an ``X-API-Key`` header."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header"
        )

    key_doc = await api_keys_collection().find_one(
        {"key_hash": hash_api_key(x_api_key), "revoked": False}
    )
    if key_doc is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )

    await api_keys_collection().update_one(
        {"_id": key_doc["_id"]},
        {"$set": {"last_used_at": datetime.now(timezone.utc)}},
    )

    user = await _load_user(str(key_doc["user_id"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )
    # Stash which key was used so the usage record can reference it, and the
    # project that key belongs to — usage inherits the key's project rather
    # than the caller declaring one, so attribution cannot drift from whichever
    # credential actually did the work.
    user["_api_key_id"] = str(key_doc["_id"])
    user["_api_key_project_id"] = key_doc.get("project_id")
    return user


async def _load_user(user_id: str | None) -> dict | None:
    if not user_id:
        return None
    try:
        oid = ObjectId(user_id)
    except (InvalidId, TypeError):
        return None
    return await users_collection().find_one({"_id": oid})
