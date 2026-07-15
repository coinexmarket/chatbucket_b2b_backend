"""API key management. Requires a Bearer JWT (dashboard).

The plaintext key is returned exactly once, at creation. Only its SHA-256 hash
is stored; listings show a masked ``cb_live_****ABCD`` form.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status

from ..database import api_keys_collection
from ..deps import get_current_user
from ..models.auth import ApiKeyCreateRequest
from ..security import generate_api_key

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _mask(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "masked_key": f"{doc.get('key_prefix', 'cb_live')}_****{doc.get('key_last4', '')}",
        "created_at": doc.get("created_at"),
        "last_used_at": doc.get("last_used_at"),
        "revoked": doc.get("revoked", False),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(
    payload: ApiKeyCreateRequest, user: dict = Depends(get_current_user)
):
    full, prefix, key_hash, last4 = generate_api_key()
    document = {
        "user_id": user["_id"],
        "name": payload.name.strip(),
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
async def list_keys(user: dict = Depends(get_current_user)):
    cursor = api_keys_collection().find({"user_id": user["_id"]}).sort("created_at", -1)
    docs = await cursor.to_list(length=None)
    return {"status": True, "data": [_mask(d) for d in docs]}


@router.delete("/{key_id}")
async def revoke_key(key_id: str, user: dict = Depends(get_current_user)):
    try:
        oid = ObjectId(key_id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Key not found."
        )
    result = await api_keys_collection().update_one(
        {"_id": oid, "user_id": user["_id"]}, {"$set": {"revoked": True}}
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Key not found."
        )
    return {"status": True, "message": "API key revoked."}
