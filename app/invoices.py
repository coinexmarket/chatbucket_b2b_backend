"""Invoice numbering and issuance.

One invoice per paid top-up, created when the gateway confirms payment.

Two properties an invoice has to hold that an ordinary record does not:

* **Gap-free, unique numbers.** Accounting expects an unbroken sequence, so the
  number comes from an atomic `$inc` on a counter document rather than a count
  of existing invoices — counting would hand the same number to two concurrent
  payments.
* **Immutability.** The customer's billing details are *snapshotted onto* the
  invoice. Referencing the profile instead would silently rewrite last year's
  invoices when someone changes address, which is exactly what an invoice
  exists to prevent.

**Tax is not computed.** `tax_status` is recorded as ``not_computed`` rather
than a zero, because a zero would assert "no tax applies" — a claim this code
is in no position to make. See the README.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import money
from .config import get_settings
from .database import counters_collection, invoices_collection
from .serialization import iso

logger = logging.getLogger("chatbucket_b2b.invoices")

_INVOICE_COUNTER = "invoice_number"


async def next_invoice_number() -> str:
    """Return the next number in the sequence, e.g. ``INV-0001``.

    Atomic: the increment and the read are one operation, so two payments
    landing at once cannot be handed the same number.
    """
    settings = get_settings()
    doc = await counters_collection().find_one_and_update(
        {"_id": _INVOICE_COUNTER},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return f"{settings.invoice_number_prefix}{doc['seq']:0{settings.invoice_number_padding}d}"


def billing_snapshot(user: dict) -> dict:
    """Copy the billing details as they stand right now.

    Falls back to the account's name/company/email so an invoice is still
    identifiable when the customer never filled the billing form in.
    """
    details = user.get("billing_details") or {}
    return {
        "legal_name": details.get("legal_name") or user.get("company") or user.get("name"),
        "email": user.get("email"),
        "gstin": details.get("gstin"),
        "address_line1": details.get("address_line1"),
        "address_line2": details.get("address_line2"),
        "city": details.get("city"),
        "state": details.get("state"),
        "postal_code": details.get("postal_code"),
        "country": details.get("country"),
        # Lets finance chase the customers whose invoices are missing the
        # details a compliant document needs, instead of finding out later.
        "complete": bool(details.get("legal_name") and details.get("address_line1")),
    }


async def issue_for_payment(payment: dict, user: dict) -> dict | None:
    """Create the invoice for a confirmed payment. Returns it, or None.

    Never raises: the payment has already succeeded and the credits are
    already granted, so a numbering hiccup must not turn a paid top-up into an
    error. A missing invoice is recoverable; a failed confirmation is not.
    """
    try:
        number = await next_invoice_number()
        document = {
            "invoice_number": number,
            "payment_id": payment["_id"],
            "user_id": payment["user_id"],
            "amount": payment.get("amount_inr"),
            "currency": payment.get("currency", get_settings().currency),
            "credits": payment.get("credit_units", 0),
            "plan": payment.get("plan"),
            "description": payment.get("description", "Credit top-up"),
            "method": payment.get("method"),
            "provider_payment_id": payment.get("provider_payment_id"),
            # Set when the gateway issues its own (GST-compliant) invoice.
            "provider_invoice_id": payment.get("provider_invoice_id"),
            "provider_invoice_url": payment.get("provider_invoice_url"),
            "bill_to": billing_snapshot(user),
            # Deliberately not a zero — see the module docstring.
            "tax_status": "not_computed",
            "status": "issued",
            "issued_at": datetime.now(timezone.utc),
        }
        result = await invoices_collection().insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except Exception as exc:
        logger.error(
            "payment %s was confirmed but no invoice could be issued: %s",
            payment.get("_id"),
            exc,
        )
        return None


def serialize(invoice: dict) -> dict:
    from . import credits

    issued = invoice.get("issued_at")
    return {
        "id": str(invoice["_id"]),
        "invoice_number": invoice.get("invoice_number"),
        "payment_id": str(invoice["payment_id"]),
        "amount": money.to_json(invoice.get("amount", 0)),
        "currency": invoice.get("currency"),
        "credits": money.to_json(credits.from_units(int(invoice.get("credits", 0)))),
        "plan": invoice.get("plan"),
        "description": invoice.get("description"),
        "method": invoice.get("method"),
        "provider_payment_id": invoice.get("provider_payment_id"),
        "provider_invoice_id": invoice.get("provider_invoice_id"),
        "provider_invoice_url": invoice.get("provider_invoice_url"),
        "bill_to": invoice.get("bill_to"),
        "tax_status": invoice.get("tax_status"),
        "status": invoice.get("status"),
        "issued_at": iso(issued),
    }
