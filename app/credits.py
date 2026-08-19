"""Credit balances and the append-only credit ledger.

Credits are the account's prepaid spending power: 1 credit = ₹1, so a usage
record costing ₹5.20 debits 5.2 credits.

**Balances are stored as integer minor units**, not `Decimal128`. 1 credit =
`UNITS_PER_CREDIT` units, matching the 4dp `money.py` works to, so the value is
exact. Integers are used because the overspend guard depends on comparing and
incrementing the balance *inside a single Mongo update* — `{$gte: n}` plus
`{$inc: -n}` — and that has to be atomic to be correct under concurrent
requests. Two simultaneous calls against a balance of 5 must not both succeed
in spending 5.

Two collections:

* ``credit_accounts`` — one document per user holding the authoritative
  balance, plus the auto-recharge settings;
* ``credit_ledger``   — append-only history of every movement, which is what
  the billing page lists and what an invoice would be rebuilt from.

The balance is the authority, not a sum of the ledger: summing an ever-growing
ledger on every metered call would get slower for exactly the customers who use
the service most.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from . import money
from .database import credit_accounts_collection, credit_ledger_collection
from .logsafe import log_safe
from .serialization import iso

logger = logging.getLogger("chatbucket_b2b.credits")

# 4 decimal places, the same precision `money.EXPONENT` quantizes to.
UNITS_PER_CREDIT = 10_000

# Ledger entry kinds.
KIND_PURCHASE = "purchase"      # paid top-up (includes any bonus)
KIND_SIGNUP_BONUS = "signup_bonus"
KIND_USAGE = "usage"            # metered consumption (negative)
KIND_ADJUSTMENT = "adjustment"  # manual correction, either sign
KIND_REFUND = "refund"


def to_units(credits: Decimal | float | int | str) -> int:
    """Credits -> integer minor units, exactly."""
    return int(money.quantize(money.to_decimal(credits)) * UNITS_PER_CREDIT)


def from_units(units: int) -> Decimal:
    """Integer minor units -> credits."""
    return money.quantize(Decimal(units) / UNITS_PER_CREDIT)


async def get_account(user_id) -> dict:
    """Return the user's credit account, creating an empty one on first touch.

    Upserted rather than created at registration so accounts that predate
    billing behave identically to new ones.
    """
    await credit_accounts_collection().update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "balance_units": 0,
                "lifetime_purchased_units": 0,
                "auto_recharge": {
                    "enabled": False,
                    "threshold_credits": None,
                    "amount_inr": None,
                },
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return await credit_accounts_collection().find_one({"user_id": user_id})


async def balance_units(user_id) -> int:
    account = await get_account(user_id)
    return int(account.get("balance_units", 0))


async def _append_ledger(
    user_id, units: int, kind: str, description: str, balance_after: int, ref=None
) -> dict:
    entry = {
        "user_id": user_id,
        "kind": kind,
        "units": units,  # signed: negative for spend
        "balance_after_units": balance_after,
        "description": description,
        "ref": ref,
        "created_at": datetime.now(timezone.utc),
    }
    result = await credit_ledger_collection().insert_one(entry)
    entry["_id"] = result.inserted_id
    return entry


async def grant(user_id, units: int, kind: str, description: str, ref=None) -> dict:
    """Add credits. Returns the ledger entry."""
    if units <= 0:
        raise ValueError("grant requires a positive number of units")

    await get_account(user_id)
    update: dict = {"$inc": {"balance_units": units}}
    if kind in (KIND_PURCHASE,):
        update["$inc"]["lifetime_purchased_units"] = units
    account = await credit_accounts_collection().find_one_and_update(
        {"user_id": user_id}, update, return_document=True
    )
    return await _append_ledger(
        user_id, units, kind, description, int(account["balance_units"]), ref
    )


async def try_debit(user_id, units: int, description: str, ref=None) -> dict | None:
    """Spend credits if the balance covers it. Returns the ledger entry, or None.

    The balance check and the decrement are a single conditional update, so two
    concurrent calls cannot both pass a check that only one balance can satisfy.
    Doing it as read-then-write would let them.
    """
    if units < 0:
        raise ValueError("try_debit requires a non-negative number of units")

    await get_account(user_id)
    account = await credit_accounts_collection().find_one_and_update(
        {"user_id": user_id, "balance_units": {"$gte": units}},
        {"$inc": {"balance_units": -units}},
        return_document=True,
    )
    if account is None:
        return None  # insufficient balance; nothing was deducted

    # The decrement above is the authoritative step and has already happened.
    # If this append fails the customer is charged with no ledger line, so it
    # is logged loudly rather than swallowed — the balance is still correct.
    try:
        return await _append_ledger(
            user_id, -units, KIND_USAGE, description, int(account["balance_units"]), ref
        )
    except Exception as exc:
        logger.error(
            "debited %s units from %s but failed to write the ledger entry: %s",
            units,
            log_safe(user_id),
            exc,
        )
        raise


async def debit_allowing_negative(user_id, units: int, description: str, ref=None) -> dict:
    """Spend credits without a balance check, letting the balance go negative.

    Used only when `ENFORCE_CREDIT_BALANCE` is off. The deployment has chosen
    never to refuse a metered call, but the consumption still happened, so the
    ledger and the balance must still record it — a negative balance is the
    honest statement of what was allowed through unpaid.
    """
    await get_account(user_id)
    account = await credit_accounts_collection().find_one_and_update(
        {"user_id": user_id}, {"$inc": {"balance_units": -units}}, return_document=True
    )
    return await _append_ledger(
        user_id, -units, KIND_USAGE, description, int(account["balance_units"]), ref
    )


def serialize_entry(entry: dict) -> dict:
    """Render a ledger entry for the billing history table."""
    created = entry.get("created_at")
    units = int(entry.get("units", 0))
    return {
        "id": str(entry["_id"]),
        "kind": entry.get("kind"),
        "credits": money.to_json(from_units(units)),
        "balance_after": money.to_json(from_units(int(entry.get("balance_after_units", 0)))),
        "description": entry.get("description"),
        "ref": str(entry["ref"]) if entry.get("ref") is not None else None,
        "created_at": iso(created),
    }
