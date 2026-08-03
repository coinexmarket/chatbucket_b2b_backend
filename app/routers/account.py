"""Account export and closure.

    GET    /account/export   everything held about the caller, as JSON
    DELETE /account          close the account

**Deletion anonymises rather than erases.** Invoices, payments, usage and the
credit ledger are financial records that most jurisdictions require to be kept
for years, so they stay — with the personal data stripped out of the user
document they point at. Erasing them would destroy the accounting trail for
money that really did change hands, which is not a right-to-be-forgotten
request, it is a bookkeeping hole.

What is removed or neutralised: name, email, phone, company, billing details,
how-they-heard, every API key (revoked, so nothing keeps working), and every
session (revoked, so nothing stays signed in).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from .. import credits, invoices, sessions
from ..database import (
    api_keys_collection,
    credit_accounts_collection,
    credit_ledger_collection,
    invoices_collection,
    payments_collection,
    projects_collection,
    usage_collection,
    users_collection,
)
from ..deps import get_current_user
from ..models.account import DeleteAccountRequest
from ..security import verify_password_async
from ..serialization import public_user, serialize_docs

router = APIRouter(prefix="/account", tags=["account"])

# Kept, but detached from any person.
_RETAINED = ("invoices", "payments", "usage", "credit_ledger")


@router.get("/export")
async def export_account(user: dict = Depends(get_current_user)):
    """Everything held about this account, in one JSON document.

    Deliberately unpaginated — an export that silently stopped at 50 records
    would be worse than none. Large accounts should use `GET /usage/export.csv`
    for the bulk of it; this is the personal-data view.
    """
    user_id = user["_id"]

    async def rows(collection, sort_field="created_at"):
        return await collection.find({"user_id": user_id}).sort(sort_field, -1).to_list(
            length=None
        )

    account = await credits.get_account(user_id)
    return {
        "status": True,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "profile": public_user(user),
            "credit_account": {
                "balance": credits.serialize_entry(
                    {
                        "_id": account["_id"],
                        "units": int(account.get("balance_units", 0)),
                        "balance_after_units": int(account.get("balance_units", 0)),
                        "kind": "balance",
                        "created_at": account.get("created_at"),
                    }
                )["credits"],
                "auto_recharge": account.get("auto_recharge"),
            },
            "api_keys": [
                # Masked: an export is a copy of your data, not a way to
                # recover a secret the service itself cannot recover.
                {
                    "id": str(d["_id"]),
                    "name": d.get("name"),
                    "masked_key": f"{d.get('key_prefix', 'cb_live')}_****{d.get('key_last4', '')}",
                    "revoked": d.get("revoked", False),
                    "created_at": d.get("created_at"),
                    "last_used_at": d.get("last_used_at"),
                }
                for d in await rows(api_keys_collection())
            ],
            "projects": serialize_docs(await rows(projects_collection())),
            "usage": serialize_docs(await rows(usage_collection())),
            "credit_ledger": [
                credits.serialize_entry(e) for e in await rows(credit_ledger_collection())
            ],
            "payments": serialize_docs(await rows(payments_collection())),
            "invoices": [
                invoices.serialize(d) for d in await rows(invoices_collection(), "issued_at")
            ],
        },
    }


@router.post("/delete")
async def delete_account(
    payload: DeleteAccountRequest, user: dict = Depends(get_current_user)
):
    """Close the account. Requires the current password.

    Password-confirmed because a stolen access token should not be enough to
    destroy an account — this is the one action with no undo.

    `POST /account/delete` rather than `DELETE /account`: a request body is the
    natural place for the password, and RFC 9110 gives DELETE bodies no defined
    semantics — several HTTP clients refuse to send one and intermediaries may
    drop it. A confirmation that silently vanishes in transit is worse than an
    unfashionable verb.
    """
    if not await verify_password_async(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Password is incorrect."
        )

    user_id = user["_id"]
    now = datetime.now(timezone.utc)

    revoked_keys = await api_keys_collection().update_many(
        {"user_id": user_id, "revoked": False}, {"$set": {"revoked": True}}
    )
    revoked_sessions = await sessions.revoke_all_for_user(user_id, "account_deleted")

    # A unique index sits on `email`, so it must be replaced rather than
    # cleared — and with something that can never be a real address, so the
    # freed-up original can be reused by a genuine future signup.
    placeholder = f"deleted+{secrets.token_hex(8)}@deleted.invalid"
    await users_collection().update_one(
        {"_id": user_id},
        {
            "$set": {
                "email": placeholder,
                "name": "Deleted account",
                "deleted_at": now,
                "updated_at": now,
                # Retire every token issued before closure.
                "email_verified": False,
            },
            "$inc": {"token_version": 1},
            "$unset": {
                "phone": "",
                "company": "",
                "how_did_you_hear": "",
                "billing_details": "",
                "reset_token_hash": "",
                "reset_token_expires": "",
                "verification_token_hash": "",
                "verification_token_expires": "",
            },
        },
    )
    # The balance is zeroed but the ledger is not: the movements that produced
    # it are part of the financial record.
    await credit_accounts_collection().update_one(
        {"user_id": user_id}, {"$set": {"balance_units": 0, "closed_at": now}}
    )

    return {
        "status": True,
        "message": "Account closed. Financial records are retained as required.",
        "api_keys_revoked": revoked_keys.modified_count,
        "sessions_revoked": revoked_sessions,
        "retained": list(_RETAINED),
    }
