"""Authentication: register, login, forgot-password, reset-password.

Tokens are JWT access tokens (see ``security.py``). Password reset produces an
opaque token whose SHA-256 hash is stored on the user with a short expiry; in a
real deployment you would email the plaintext token as a reset link. In
development the token is returned in the response for convenience.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from .. import credits, ratelimit, sessions, verification
from ..config import get_settings
from ..database import indexes_ready, users_collection
from ..deps import get_current_user
from ..email import (
    send_email_verification,
    send_email_verified,
    send_password_reset,
    send_welcome,
)
from ..models.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    VerifyEmailOtpRequest,
    VerifyEmailRequest,
)
from ..plans import DEFAULT_PLAN
from ..security import (
    create_access_token_for_user,
    dummy_password_hash,
    generate_reset_token,
    hash_email_otp,
    hash_password_async,
    hash_token,
    verify_password_async,
)
from ..serialization import public_user

logger = logging.getLogger("chatbucket_b2b.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


async def _token_response(user: dict, family: str | None = None) -> dict:
    settings = get_settings()
    refresh_token, refresh_expires = await sessions.issue(user["_id"], family)
    return {
        "access_token": create_access_token_for_user(user),
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
        # Lets the dashboard renew a 24h access token instead of dumping the
        # user back at the sign-in screen mid-session.
        "refresh_token": refresh_token,
        "refresh_expires_at": refresh_expires.isoformat(),
        "user": public_user(user),
    }


_DUPLICATE_EMAIL = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="An account with this email already exists.",
)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ratelimit.by_ip("register_ip"))],
)
async def register(payload: RegisterRequest, background_tasks: BackgroundTasks):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    email = payload.email.lower().strip()

    # The unique email index is what really enforces this, including for two
    # concurrent signups. While it is missing, that guarantee is gone entirely,
    # so fall back to an explicit lookup rather than silently accepting a
    # duplicate account (which would then make `login` pick an arbitrary one).
    if not indexes_ready() and await users_collection().find_one({"email": email}):
        raise _DUPLICATE_EMAIL

    how_heard = (payload.how_did_you_hear or "").strip() or None
    document = {
        "name": payload.name.strip(),
        "email": email,
        "company": payload.company.strip() if payload.company else None,
        # Stored under `phone` — the field the user document and `PUT /profile`
        # already use — so the signup form's "Mobile Number" does not become a
        # second column meaning the same thing. Normalised to E.164 by the model.
        "phone": payload.mobile,
        "how_did_you_hear": how_heard,
        # When they agreed, and to what. A boolean alone cannot answer either
        # question later, and the terms text will change.
        "terms_accepted_at": now,
        "terms_version": settings.terms_version,
        "plan": DEFAULT_PLAN,
        "email_verified": False,
        "password_hash": await hash_password_async(payload.password),
        "token_version": 0,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await users_collection().insert_one(document)
    except DuplicateKeyError:
        raise _DUPLICATE_EMAIL
    document["_id"] = result.inserted_id

    # Open the credit account, and grant the trial balance if one is
    # configured. Failing here must not undo a successful signup, so the
    # account is left to be created lazily on first use instead.
    bonus_units = credits.to_units(settings.signup_bonus_credits)
    try:
        if bonus_units > 0:
            await credits.grant(
                document["_id"],
                bonus_units,
                credits.KIND_SIGNUP_BONUS,
                "Welcome credits",
            )
        else:
            await credits.get_account(document["_id"])
    except Exception as exc:
        logger.error("could not open credit account for %s: %s", document["_id"], exc)

    # Queued after the response, like the reset email.
    token, code = await _issue_verification(document, background_tasks)

    # Also queued: a welcome email is worth sending, but not worth making the
    # customer wait on an SMTP round trip before their account appears.
    background_tasks.add_task(
        send_welcome,
        document["email"],
        document["name"],
        # None rather than "0" when nothing was granted, so the template hides
        # the free-credits panel instead of advertising zero.
        _bonus_label(bonus_units),
    )

    body = await _token_response(document)
    if settings.is_dev:
        # No inbox to read the link or the code out of under the console backend.
        body["verification_token"] = token
        body["verification_code"] = code
    return body


def _bonus_label(bonus_units: int) -> str | None:
    """The trial balance as the welcome email should show it, or None."""
    if bonus_units <= 0:
        return None
    amount = credits.from_units(bonus_units)
    # Whole rupees read better than "100.0000" on a marketing panel.
    return f"{amount:,.0f}" if amount == amount.to_integral_value() else f"{amount:,.2f}"


@router.post("/login", dependencies=[Depends(ratelimit.by_ip("login_ip"))])
async def login(payload: LoginRequest, request: Request):
    email = payload.email.lower().strip()
    # Also limited per account. Per-IP alone lets a botnet spread an attack on
    # one account across many addresses; per-email alone lets one address work
    # through many accounts. Both are needed.
    await ratelimit.enforce("login_email", email, "login_email")

    user = await users_collection().find_one({"email": email})

    # Always run the verify, against a throwaway hash when there is no such
    # user. Returning early instead would skip ~240k PBKDF2 rounds and answer
    # noticeably faster, letting anyone time this endpoint to discover which
    # emails have accounts — the same leak `forgot-password` below avoids.
    stored = user["password_hash"] if user is not None else dummy_password_hash()
    password_ok = await verify_password_async(payload.password, stored)

    if user is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    return await _token_response(user)


@router.post("/forgot-password", dependencies=[Depends(ratelimit.by_ip("forgot_ip"))])
async def forgot_password(
    payload: ForgotPasswordRequest, background_tasks: BackgroundTasks
):
    settings = get_settings()
    email = payload.email.lower().strip()
    # Caps reset emails per address, so this endpoint cannot be used to mail-
    # bomb someone. Applied before the lookup so the limit is identical whether
    # or not the account exists — a limit that only bit real accounts would
    # itself leak which addresses are registered.
    await ratelimit.enforce("forgot_email", email, "forgot_email")

    user = await users_collection().find_one({"email": email})

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
        # Queued rather than awaited: the response goes out first, so a
        # registered address does not take measurably longer than an unknown
        # one. Awaiting the SMTP round trip here would undo the identical
        # responses above and hand back an enumeration oracle.
        background_tasks.add_task(
            send_password_reset, user["email"], token, user.get("name")
        )

        # Still returned in development, where the console backend means there
        # is no inbox to read the link out of.
        if settings.is_dev:
            response["reset_token"] = token

    return JSONResponse(status_code=200, content=response)


async def _issue_verification(
    user: dict, background_tasks: BackgroundTasks
) -> tuple[str, str]:
    """Mint a fresh link token and code, queue the email, return both.

    The credentials themselves are minted by `verification.issue_credentials`,
    which the scheduled reminder also uses — a reminder a day later has to mint
    a *new* pair, because the code from signup expired within minutes.
    """
    token, code = await verification.issue_credentials(user["_id"])
    background_tasks.add_task(
        send_email_verification, user["email"], token, code, user.get("name")
    )
    return token, code


async def _confirm_and_notify(user: dict, background_tasks: BackgroundTasks) -> None:
    """Mark the address verified and tell the customer their account is open.

    The confirmation is queued after the response for the same reason every
    other message is: verification should not wait on a mail server.
    """
    await users_collection().update_one(
        {"_id": user["_id"]}, verification.mark_verified_update()
    )
    # State the balance they can now actually spend — until this moment the
    # signup credits were granted but unusable.
    try:
        balance = credits.from_units(await credits.balance_units(user["_id"]))
        available = f"{balance:,.0f}" if balance == balance.to_integral_value() else f"{balance:,.2f}"
    except Exception as exc:
        logger.error("could not read balance for %s: %s", user["_id"], exc)
        available = None
    background_tasks.add_task(
        send_email_verified, user["email"], user.get("name"), available
    )


def _as_utc(moment):
    """Mongo stores UTC; some driver configs hand back naive datetimes."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


@router.post("/verify-email")
async def verify_email(payload: VerifyEmailRequest, background_tasks: BackgroundTasks):
    """Confirm an email address from the link's token.

    Public and idempotent-ish: verifying an already-verified account with a
    spent token is a 400, but the account stays verified either way.
    """
    user = await users_collection().find_one(
        {"verification_token_hash": hash_token(payload.token)}
    )
    expires = _as_utc(user.get("verification_token_expires") if user else None)
    if user is None or expires is None or expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    await _confirm_and_notify(user, background_tasks)
    return {"status": True, "message": "Email verified."}


@router.post(
    "/verify-email/otp",
    dependencies=[Depends(ratelimit.by_ip("verify_otp_ip"))],
)
async def verify_email_otp(
    payload: VerifyEmailOtpRequest, background_tasks: BackgroundTasks
):
    """Confirm an email address from the six-digit code in the email.

    Public, like the link route — someone verifying on a phone has no session.
    Two things stop that being a way to brute-force six digits: a per-address
    attempt counter that burns the code, and a per-IP limit on top.
    """
    settings = get_settings()
    email = payload.email.lower().strip()
    # Also limited per address, for the same reason `login` is: a per-IP cap
    # alone lets a spread-out attacker work through one account's million codes.
    await ratelimit.enforce("verify_otp_email", email, "verify_otp_email")

    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired verification code.",
    )

    user = await users_collection().find_one({"email": email})
    if user is None:
        raise invalid
    if user.get("email_verified"):
        # Idempotent for the customer who taps the button twice, and it reveals
        # nothing: they already told us this address by typing it.
        return {"status": True, "message": "Email is already verified."}

    stored = user.get("verification_code_hash")
    expires = _as_utc(user.get("verification_code_expires"))
    attempts = int(user.get("verification_code_attempts", 0))
    if not stored or expires is None or expires < datetime.now(timezone.utc):
        raise invalid
    if attempts >= settings.email_otp_max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect codes. Request a new one.",
        )

    if not secrets.compare_digest(stored, hash_email_otp(payload.code)):
        # Counted in the database, not in memory: the cap has to hold across
        # workers and restarts, and an in-process counter holds across neither.
        await users_collection().update_one(
            {"_id": user["_id"]}, {"$inc": {"verification_code_attempts": 1}}
        )
        raise invalid

    await _confirm_and_notify(user, background_tasks)
    return {"status": True, "message": "Email verified."}


@router.post("/verify-email/resend")
async def resend_verification(
    background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)
):
    """Send a fresh verification link to the signed-in account's address.

    Authenticated rather than taking an email, so it cannot be used to mail
    an address the caller does not control.
    """
    settings = get_settings()
    if user.get("email_verified"):
        return {"status": True, "message": "Email is already verified."}

    token, code = await _issue_verification(user, background_tasks)
    response: dict = {"status": True, "message": "Verification email sent."}
    if settings.is_dev:
        response["verification_token"] = token
        response["verification_code"] = code
    return response


@router.post("/refresh")
async def refresh(payload: RefreshRequest):
    """Exchange a refresh token for a fresh access token.

    The refresh token is rotated: the one presented is spent and a new one
    returned, so each has exactly one valid use. Presenting a spent token means
    the value leaked, and every session descended from that sign-in is revoked
    — see `sessions.consume`.
    """
    record = await sessions.consume(payload.refresh_token)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid, expired or already used.",
        )

    user = await users_collection().find_one({"_id": record["user_id"]})
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found."
        )
    # Chained to the original sign-in so a later reuse can revoke the family.
    return await _token_response(user, family=record["family"])


@router.post("/logout")
async def logout(payload: LogoutRequest, user: dict = Depends(get_current_user)):
    """Sign out. Revokes the given refresh token, or every session.

    Access tokens already issued stay valid until they expire — they are
    self-contained by design. `all_sessions` additionally bumps `token_version`,
    which retires those immediately, at the cost of signing the caller out too.
    """
    if payload.all_sessions:
        revoked = await sessions.revoke_all_for_user(user["_id"])
        await users_collection().update_one(
            {"_id": user["_id"]}, {"$inc": {"token_version": 1}}
        )
        return {
            "status": True,
            "message": "Signed out of all sessions.",
            "sessions_revoked": revoked,
        }

    if payload.refresh_token:
        await sessions.revoke_one(payload.refresh_token)
    return {"status": True, "message": "Signed out.", "sessions_revoked": 1}


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

    new_password_hash = await hash_password_async(payload.new_password)
    await users_collection().update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": new_password_hash,
                "updated_at": datetime.now(timezone.utc),
            },
            # Retire every token issued before this reset. A reset is what you
            # do when the account is compromised, so leaving the attacker's
            # existing session alive would defeat the point.
            "$inc": {"token_version": 1},
            # `token_version` only retires access tokens; without this the
            # attacker's refresh token would still mint new ones.
            "$unset": {"reset_token_hash": "", "reset_token_expires": ""},
        },
    )
    await sessions.revoke_all_for_user(user["_id"], "password_reset")
    return {"status": True, "message": "Password has been reset."}
