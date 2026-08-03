"""Profile: view, update details, change password. Requires a Bearer JWT."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from .. import sessions
from ..database import users_collection
from ..deps import get_current_user
from ..models.auth import ChangePasswordRequest, ProfileUpdateRequest
from ..security import (
    create_access_token_for_user,
    hash_password_async,
    verify_password_async,
)
from ..serialization import public_user

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("")
async def get_profile(user: dict = Depends(get_current_user)):
    return {"status": True, "data": public_user(user)}


@router.put("")
async def update_profile(
    payload: ProfileUpdateRequest, user: dict = Depends(get_current_user)
):
    updates = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update."
        )
    updates["updated_at"] = datetime.now(timezone.utc)
    await users_collection().update_one({"_id": user["_id"]}, {"$set": updates})
    fresh = await users_collection().find_one({"_id": user["_id"]})
    return {"status": True, "data": public_user(fresh)}


@router.put("/password")
async def change_password(
    payload: ChangePasswordRequest, user: dict = Depends(get_current_user)
):
    if not await verify_password_async(payload.current_password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    new_password_hash = await hash_password_async(payload.new_password)
    await users_collection().update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": new_password_hash,
                "updated_at": datetime.now(timezone.utc),
            },
            # Sign out every other session that is holding an older token.
            "$inc": {"token_version": 1},
        },
    )

    # Refresh tokens survive a `token_version` bump, so revoke them too —
    # otherwise a stolen session could mint new access tokens indefinitely.
    await sessions.revoke_all_for_user(user["_id"], "password_change")

    # The bump above also retires the caller's own token, so hand back a fresh
    # one — changing your password shouldn't log you out of the tab you're in.
    fresh = await users_collection().find_one({"_id": user["_id"]})
    return {
        "status": True,
        "message": "Password updated. Other sessions have been signed out.",
        "access_token": create_access_token_for_user(fresh),
    }
