"""Email-verification credentials — issuing them, and what they cost.

Two credentials are issued together for one job, with deliberately different
lifetimes: a link that lasts `VERIFICATION_TOKEN_EXPIRE_HOURS` because it may
be opened from a mail client days later, and a six-digit code that lasts
`EMAIL_OTP_EXPIRE_MINUTES` because six digits is only a secret while the window
to guess it is small.

This lives in its own module rather than inside the auth router because two
callers need it: `POST /auth/register` (and the resend endpoint), and the
scheduled reminder that chases accounts which never verified. A reminder sent a
day later **must mint a fresh pair** — the code from signup expired within
minutes, and re-sending a dead credential is worse than sending nothing,
because the customer tries it and concludes the product is broken.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .database import phone_verifications_collection, users_collection
from .security import (
    generate_email_otp,
    generate_verification_token,
    hash_email_otp,
)

# Applied by every path that confirms an address, so a code and a link cannot
# leave the account in two different states.
MARK_VERIFIED = {
    "$set": {"email_verified": True},
    # Single use: both credentials are spent whichever one was presented.
    "$unset": {
        "verification_token_hash": "",
        "verification_token_expires": "",
        "verification_code_hash": "",
        "verification_code_expires": "",
        "verification_code_attempts": "",
    },
}


def mark_verified_update(now: datetime | None = None) -> dict:
    """The Mongo update that confirms an address, stamped with the time."""
    moment = now or datetime.now(timezone.utc)
    return {
        **MARK_VERIFIED,
        "$set": {**MARK_VERIFIED["$set"], "email_verified_at": moment},
    }


CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"


def channel_for(phone: str | None) -> str:
    """Which channel verifies this account: ``sms`` or ``email``.

    An Indian number verifies by SMS and skips the email code entirely; every
    other country verifies by email. This is the **single** place that rule
    lives, so no router has to know about dial codes — and so changing the rule
    is one function rather than a hunt.

    `SMS_COUNTRY_CODES` drives it, defaulting to `+91`. The number is already
    E.164 by the time it is stored (`models/auth.normalize_phone`), so a prefix
    test is exact rather than a guess.
    """
    settings = get_settings()
    if settings.resolved_sms_backend == "disabled":
        # Nothing can be texted, so the only channel that works is email.
        return CHANNEL_EMAIL
    if not phone:
        return CHANNEL_EMAIL
    number = phone.strip()
    if any(number.startswith(code) for code in settings.sms_country_code_list):
        return CHANNEL_SMS
    return CHANNEL_EMAIL


def is_verified(user: dict) -> bool:
    """True when the account has confirmed itself through **either** channel.

    The gate on API-key creation reads this rather than `email_verified`
    directly: an Indian account is never sent an email code, so checking that
    field alone would block it permanently the moment
    `REQUIRE_EMAIL_VERIFICATION` is turned on.

    Deliberately either/or rather than "whichever channel applies now".
    `channel_for` depends on the *current* phone number, so re-deriving it here
    would mean an email-verified customer who edits their phone to an Indian
    one silently becomes unverified and loses the ability to create a key —
    punished for updating their profile. What was proven stays proven; the
    channel rule decides which code to *send*, not which flag to trust forever.

    Changing the phone number does still clear `phone_verified` (see
    `routers/profile.py`), because the new number is not the proven one.
    """
    return bool(user.get("email_verified")) or bool(user.get("phone_verified"))


MARK_PHONE_VERIFIED = {
    "$set": {"phone_verified": True},
    "$unset": {
        "phone_code_hash": "",
        "phone_code_expires": "",
        "phone_code_attempts": "",
    },
}


def mark_phone_verified_update(now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    return {
        **MARK_PHONE_VERIFIED,
        "$set": {**MARK_PHONE_VERIFIED["$set"], "phone_verified_at": moment},
    }


async def issue_phone_code(user_id) -> str:
    """Store a fresh six-digit code for the mobile number. Returns the code.

    Separate storage from the email code on purpose: an account could hold both
    at once (a number that changed country, say), and one attempt counter
    guarding two secrets would let the unguarded one be brute-forced freely.
    """
    settings = get_settings()
    code, code_hash = generate_email_otp()
    await users_collection().update_one(
        {"_id": user_id},
        {
            "$set": {
                "phone_code_hash": code_hash,
                "phone_code_expires": datetime.now(timezone.utc)
                + timedelta(minutes=settings.phone_otp_expire_minutes),
                # Reset with the code: a resend is a new secret, and carrying
                # the old count over would lock someone out of a code they have
                # only just been sent.
                "phone_code_attempts": 0,
            }
        },
    )
    return code


# --- Numbers proven before the account exists ------------------------------
#
# The signup form verifies the mobile number *while the form is being filled
# in*, so the code has to be issued and checked before there is any user
# document to store it on. These four functions are that flow. They mirror the
# three above, keyed by the number instead of by `_id`.
#
# The order matters for a reason worth stating: verifying first means an account
# is only ever created for a number somebody can actually receive a text on, and
# the ₹-per-message cost is paid before the free signup credits are granted
# rather than after. Verifying afterwards would let a signup with a mistyped
# number succeed and take the bonus with it.

OUTCOME_OK = "ok"
OUTCOME_INVALID = "invalid"
OUTCOME_LOCKED = "locked"


async def issue_pending_phone_code(phone: str) -> str:
    """Store a fresh code against a bare number. Returns the code.

    Upsert rather than insert: a resend must replace the previous code, not add
    a second live one (see the unique index on `phone`).
    """
    settings = get_settings()
    code, code_hash = generate_email_otp()
    now = datetime.now(timezone.utc)
    await phone_verifications_collection().update_one(
        {"phone": phone},
        {
            "$set": {
                "code_hash": code_hash,
                "expires_at": now
                + timedelta(minutes=settings.phone_otp_expire_minutes),
                "attempts": 0,
                "updated_at": now,
                # Long enough to cover the code window *and* the grace period a
                # verified record stays usable for, so the TTL index never
                # deletes a record that registration is about to read.
                "purge_at": now
                + timedelta(
                    minutes=settings.phone_otp_expire_minutes
                    + settings.phone_verification_grace_minutes
                    + 60
                ),
            },
            "$setOnInsert": {"phone": phone, "created_at": now},
            # A new code un-verifies the number: otherwise someone could verify,
            # then request a code for the same number and still be treated as
            # verified without ever proving the second one.
            "$unset": {"verified_at": ""},
        },
        upsert=True,
    )
    return code


async def check_pending_phone_code(phone: str, code: str) -> str:
    """Check a code against a bare number. Returns one of the ``OUTCOME_*``.

    On success the record is stamped `verified_at` and the code is spent, so
    `phone_recently_verified` can answer for it during registration.
    """
    settings = get_settings()
    record = await phone_verifications_collection().find_one({"phone": phone})
    if record is None:
        return OUTCOME_INVALID

    stored = record.get("code_hash")
    expires = record.get("expires_at")
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not stored or expires is None or expires < datetime.now(timezone.utc):
        return OUTCOME_INVALID
    if int(record.get("attempts", 0)) >= settings.phone_otp_max_attempts:
        return OUTCOME_LOCKED

    if not secrets.compare_digest(stored, hash_email_otp(code)):
        # In the database, not in memory, so the cap survives a restart and
        # holds across every worker rather than per-process.
        await phone_verifications_collection().update_one(
            {"phone": phone}, {"$inc": {"attempts": 1}}
        )
        return OUTCOME_INVALID

    await phone_verifications_collection().update_one(
        {"phone": phone},
        {
            "$set": {"verified_at": datetime.now(timezone.utc)},
            # Single use, like every other code here.
            "$unset": {"code_hash": "", "expires_at": "", "attempts": ""},
        },
    )
    return OUTCOME_OK


async def phone_recently_verified(phone: str) -> bool:
    """True when this number was proven a short while ago and not yet claimed.

    Bounded by `PHONE_VERIFICATION_GRACE_MINUTES` rather than open-ended: the
    proof is that somebody held the handset *at that moment*, and a record left
    lying around for a day would let a number verified once be attached to an
    account created much later by somebody else.
    """
    settings = get_settings()
    record = await phone_verifications_collection().find_one({"phone": phone})
    if record is None:
        return False
    verified_at = record.get("verified_at")
    if verified_at is None:
        return False
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.phone_verification_grace_minutes
    )
    return verified_at >= cutoff


async def claim_pending_phone(phone: str) -> None:
    """Consume the record once an account has been created for the number.

    Single use: without this the same proof could be replayed to create several
    accounts on one number, each collecting the signup bonus — the exact abuse
    the unique index on `users.phone` exists to stop.
    """
    await phone_verifications_collection().delete_one({"phone": phone})


async def issue_credentials(user_id) -> tuple[str, str]:
    """Store a fresh link token and code on the account. Returns ``(token, code)``.

    Replaces whatever was there. Issuing supersedes rather than adds: an
    account should never have two live codes, or the attempt counter below
    guards one of them while the other stays freely guessable.
    """
    settings = get_settings()
    token, token_hash = generate_verification_token()
    code, code_hash = generate_email_otp()
    now = datetime.now(timezone.utc)

    await users_collection().update_one(
        {"_id": user_id},
        {
            "$set": {
                "verification_token_hash": token_hash,
                "verification_token_expires": now
                + timedelta(hours=settings.verification_token_expire_hours),
                "verification_code_hash": code_hash,
                "verification_code_expires": now
                + timedelta(minutes=settings.email_otp_expire_minutes),
                # Reset with each new code: a resend is a fresh secret, and
                # carrying the old attempt count over would lock someone out
                # of a code they have only just received.
                "verification_code_attempts": 0,
            }
        },
    )
    return token, code
