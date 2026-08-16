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

from datetime import datetime, timedelta, timezone

from .config import get_settings
from .database import users_collection
from .security import generate_email_otp, generate_verification_token

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
