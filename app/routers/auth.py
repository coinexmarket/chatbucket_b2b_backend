"""Authentication: register, login, forgot-password, reset-password.

Tokens are JWT access tokens (see ``security.py``). Password reset produces an
opaque token whose SHA-256 hash is stored on the user with a short expiry; in a
real deployment you would email the plaintext token as a reset link. In
development the token is returned in the response for convenience.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from ..config import get_settings
from ..database import indexes_ready, users_collection
from ..models.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from ..security import (
    create_access_token_for_user,
    dummy_password_hash,
    generate_reset_token,
    hash_password,
    hash_token,
    verify_password,
)
from ..serialization import public_user

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: dict) -> dict:
    settings = get_settings()
    return {
        "access_token": create_access_token_for_user(user),
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
        "user": public_user(user),
    }


_DUPLICATE_EMAIL = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="An account with this email already exists.",
)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest):
    now = datetime.now(timezone.utc)
    email = payload.email.lower().strip()

    # The unique email index is what really enforces this, including for two
    # concurrent signups. While it is missing, that guarantee is gone entirely,
    # so fall back to an explicit lookup rather than silently accepting a
    # duplicate account (which would then make `login` pick an arbitrary one).
    if not indexes_ready() and await users_collection().find_one({"email": email}):
        raise _DUPLICATE_EMAIL

    document = {
        "name": payload.name.strip(),
        "email": email,
        "company": payload.company.strip() if payload.company else None,
        "phone": None,
        "password_hash": hash_password(payload.password),
        "token_version": 0,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await users_collection().insert_one(document)
    except DuplicateKeyError:
        raise _DUPLICATE_EMAIL
    document["_id"] = result.inserted_id
    return _token_response(document)


@router.post("/login")
async def login(payload: LoginRequest):
    user = await users_collection().find_one({"email": payload.email.lower().strip()})

    # Always run the verify, against a throwaway hash when there is no such
    # user. Returning early instead would skip ~240k PBKDF2 rounds and answer
    # noticeably faster, letting anyone time this endpoint to discover which
    # emails have accounts — the same leak `forgot-password` below avoids.
    stored = user["password_hash"] if user is not None else dummy_password_hash()
    password_ok = verify_password(payload.password, stored)

    if user is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    return _token_response(user)


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest):
    settings = get_settings()
    user = await users_collection().find_one({"email": payload.email.lower().strip()})

    # Always return the same response so we don't leak which emails exist.
    response: dict = {
        "status": True,
        "message": "If that email is registered, a reset link has been sent.",
    }

    if user is not None:
        token, token_hash = generate_reset_token()
        expires = datetime.now(timezone.utc) + timedelta(
            minutes=settings.reset_token_expire_minutes
        )
        await users_collection().update_one(
            {"_id": user["_id"]},
            {"$set": {"reset_token_hash": token_hash, "reset_token_expires": expires}},
        )
        # TODO: email the token as a reset link. Exposed only in development.
        if settings.is_dev:
            response["reset_token"] = token

    return JSONResponse(status_code=200, content=response)


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest):
    user = await users_collection().find_one(
        {"reset_token_hash": hash_token(payload.token)}
    )
    expires = user.get("reset_token_expires") if user else None
    # Mongo stores UTC; some driver configs return naive datetimes — coerce so
    # the comparison never mixes offset-aware and offset-naive values.
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if user is None or expires is None or expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    await users_collection().update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": hash_password(payload.new_password),
                "updated_at": datetime.now(timezone.utc),
            },
            # Retire every token issued before this reset. A reset is what you
            # do when the account is compromised, so leaving the attacker's
            # existing session alive would defeat the point.
            "$inc": {"token_version": 1},
            "$unset": {"reset_token_hash": "", "reset_token_expires": ""},
        },
    )
    return {"status": True, "message": "Password has been reset."}
