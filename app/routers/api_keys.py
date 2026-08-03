"""API key management. Requires a Bearer JWT (dashboard).

The plaintext key is returned exactly once, at creation. Only its SHA-256 hash
is stored; listings show a masked ``cb_live_****ABCD`` form.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from .. import credits, money
from ..config import get_settings
from ..database import api_keys_collection
from ..deps import get_api_user, get_current_user
from ..models.auth import ApiKeyCreateRequest, ApiKeyRenameRequest
from ..plans import get_plan
from ..security import generate_api_key
from .projects import resolve_project

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _mask(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "masked_key": f"{doc.get('key_prefix', 'cb_live')}_****{doc.get('key_last4', '')}",
        "project_id": doc.get("project_id"),
        "created_at": doc.get("created_at"),
        "last_used_at": doc.get("last_used_at"),
        "revoked": doc.get("revoked", False),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(
    payload: ApiKeyCreateRequest, user: dict = Depends(get_current_user)
):
    # An unverified address means nobody has proven they own it, so issuing a
    # live credential against it is a decision worth gating. Off by default:
    # switching it on would lock out every account created before verification
    # existed until they confirm.
    if get_settings().require_email_verification and not user.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify your email address before creating API keys.",
        )

    full, prefix, key_hash, last4 = generate_api_key()
    # Validated against the caller's own projects, so a guessed id cannot
    # attach this key to another customer's project.
    project_id = await resolve_project(user, payload.project_id)
    document = {
        "user_id": user["_id"],
        "name": payload.name.strip(),
        "project_id": project_id,
        "key_prefix": prefix,
        "key_hash": key_hash,
        "key_last4": last4,
        "revoked": False,
        "created_at": datetime.now(timezone.utc),
        "last_used_at": None,
    }
    result = await api_keys_collection().insert_one(document)
    document["_id"] = result.inserted_id
    return {
        "status": True,
        "message": "Store this key now — it will not be shown again.",
        "api_key": full,
        "data": _mask(document),
    }


@router.get("")
async def list_keys(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_revoked: bool = Query(default=True),
):
    """The caller's API keys, newest first.

    Paged rather than returning everything: an account that has rotated keys
    for years would otherwise get the whole history in one response.
    """
    query: dict = {"user_id": user["_id"]}
    if not include_revoked:
        query["revoked"] = False

    total = await api_keys_collection().count_documents(query)
    cursor = (
        api_keys_collection()
        .find(query)
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    return {
        "status": True,
        "count": len(docs),
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [_mask(d) for d in docs],
    }


def _key_oid(key_id: str) -> ObjectId:
    try:
        return ObjectId(key_id)
    except (InvalidId, TypeError):
        # A malformed id is indistinguishable from someone else's key here, and
        # both should look the same to the caller.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Key not found."
        )


@router.patch("/{key_id}")
async def rename_key(
    key_id: str,
    payload: ApiKeyRenameRequest,
    user: dict = Depends(get_current_user),
):
    """Rename a key. The secret itself is unchanged — this is only the label."""
    updates: dict = {"name": payload.name.strip()}
    if payload.project_id is not None:
        # "" means unassign; an id is validated as the caller's own.
        updates["project_id"] = await resolve_project(user, payload.project_id or None)

    doc = await api_keys_collection().find_one_and_update(
        # Scoped to the caller's own keys, so a valid id belonging to another
        # customer is a 404 rather than a rename of their key.
        {"_id": _key_oid(key_id), "user_id": user["_id"]},
        {"$set": updates},
        return_document=True,
    )
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Key not found."
        )
    return {"status": True, "message": "API key renamed.", "data": _mask(doc)}


@router.post("/verify")
async def verify_key(
    caller: dict = Depends(get_api_user),
    response: Response = None,  # type: ignore[assignment]
):
    """Validate an ``X-API-Key`` and say whose it is. For our AI services.

    The STT/TTS/translation/voice services need to answer two questions before
    doing any work: is this a real customer, and which one. Until now they had
    neither — they authenticated with one shared secret every customer
    presented, so the caller's identity was not knowable and usage could not be
    attributed to anyone. This is the endpoint that makes per-customer metering
    possible.

    Deliberately a **POST**: the key travels in a header either way, but a GET
    invites caching by a proxy, and a cached "valid" answer would outlive a
    revoked key.

    Returns the plan and remaining credits too, so a service can refuse work a
    customer cannot pay for rather than doing it and discovering that at
    metering time — the point at which refusing is too late to save the cost.
    """
    key_id = caller.get("_api_key_id")
    account = await credits.get_account(caller["_id"])
    balance = credits.from_units(int(account.get("balance_units", 0)))
    plan = get_plan(caller.get("plan"))

    # Answered fresh every time: a revoked key must stop working immediately,
    # which is the whole reason this is not a cacheable GET.
    if response is not None:
        response.headers["Cache-Control"] = "no-store"

    return {
        "status": True,
        "data": {
            "user_id": str(caller["_id"]),
            "api_key_id": key_id,
            "project_id": caller.get("_api_key_project_id"),
            "plan": plan.key,
            "requests_per_minute": plan.requests_per_minute,
            "credits": money.to_json(balance),
            # False means the next metered call will 402. A service can stop
            # here instead of doing work it will not be paid for.
            "has_credits": balance > 0,
        },
    }


@router.delete("/{key_id}")
async def revoke_key(key_id: str, user: dict = Depends(get_current_user)):
    oid = _key_oid(key_id)
    result = await api_keys_collection().update_one(
        {"_id": oid, "user_id": user["_id"]}, {"$set": {"revoked": True}}
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Key not found."
        )
    return {"status": True, "message": "API key revoked."}
