"""Credits and billing — backs the dashboard's Billing page.

    GET  /billing                    balance + auto-recharge settings
    GET  /billing/history            credit ledger (what the table lists)
    GET  /billing/payments           top-up orders and their status
    PUT  /billing/auto-recharge      configure low-balance top-up
    POST /billing/top-up             start a top-up, returns a pending payment
    POST /billing/payments/{id}/confirm   gateway webhook: mark paid, credit

Credits are granted **only** by the confirm endpoint, which requires the
gateway's shared secret. There is deliberately no authenticated route by which
a customer can add credits to their own account — that would be a licence to
print money.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pymongo.errors import DuplicateKeyError

from .. import credits, email, invoices, money
from .. import payments as payments_gateway
from ..config import get_settings
from ..database import (
    credit_accounts_collection,
    credit_ledger_collection,
    invoices_collection,
    payments_collection,
    users_collection,
)
from ..deps import get_current_user
from ..models.billing import (
    AutoRechargeRequest,
    BillingDetailsRequest,
    PaymentConfirmation,
    CheckoutCallback,
    TopUpRequest,
)
from ..plans import PURCHASABLE, get_plan
from ..serialization import iso

logger = logging.getLogger("chatbucket_b2b.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


def _account_view(account: dict, plan_key: str | None) -> dict:
    plan = get_plan(plan_key)
    balance = int(account.get("balance_units", 0))
    purchased = int(account.get("lifetime_purchased_units", 0))
    auto = account.get("auto_recharge") or {}
    return {
        "plan": plan.key,
        "plan_label": plan.label,
        "currency": get_settings().currency,
        "credits": money.to_json(credits.from_units(balance)),
        "lifetime_purchased_credits": money.to_json(credits.from_units(purchased)),
        # The progress bar reads "x of y used": y is everything ever bought,
        # which is the only total that does not shrink as credits are spent.
        "credits_used": money.to_json(credits.from_units(max(purchased - balance, 0))),
        "auto_recharge": {
            "enabled": bool(auto.get("enabled", False)),
            "threshold_credits": auto.get("threshold_credits"),
            "amount_inr": auto.get("amount_inr"),
        },
    }


@router.get("")
async def get_billing(user: dict = Depends(get_current_user)):
    account = await credits.get_account(user["_id"])
    return {"status": True, "data": _account_view(account, user.get("plan"))}


@router.get("/history")
async def get_history(
    user: dict = Depends(get_current_user),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """The credit ledger: purchases, bonuses and metered spend."""
    query: dict = {"user_id": user["_id"]}
    if kind:
        query["kind"] = kind
    cursor = credit_ledger_collection().find(query).sort("created_at", -1).limit(limit)
    entries = await cursor.to_list(length=limit)
    return {
        "status": True,
        "count": len(entries),
        "data": [credits.serialize_entry(e) for e in entries],
    }


def _payment_view(doc: dict) -> dict:
    created = doc.get("created_at")
    paid = doc.get("paid_at")
    return {
        "id": str(doc["_id"]),
        "amount": money.to_json(doc.get("amount_inr", 0)),
        "currency": doc.get("currency", "INR"),
        "credits": money.to_json(credits.from_units(int(doc.get("credit_units", 0)))),
        "plan": doc.get("plan"),
        "status": doc.get("status"),
        "method": doc.get("method"),
        # Populated once the payment is confirmed; this is what the billing
        # history's "Invoice" column shows.
        "invoice_number": doc.get("invoice_number"),
        "provider_order_id": doc.get("provider_order_id"),
        "created_at": iso(created),
        "paid_at": iso(paid) if paid else None,
    }


@router.get("/payments")
async def list_payments(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
):
    cursor = payments_collection().find({"user_id": user["_id"]}).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return {"status": True, "count": len(docs), "data": [_payment_view(d) for d in docs]}


@router.get("/details")
async def get_billing_details(user: dict = Depends(get_current_user)):
    """The customer's invoicing identity, or nulls if never filled in."""
    return {"status": True, "data": user.get("billing_details")}


@router.put("/details")
async def set_billing_details(
    payload: BillingDetailsRequest, user: dict = Depends(get_current_user)
):
    """Set the details that will be frozen onto future invoices.

    Editing these never alters an invoice already issued: each one carries its
    own snapshot, so history stays a record of what was true at the time.
    """
    details = payload.model_dump()
    await users_collection().update_one(
        {"_id": user["_id"]},
        {"$set": {"billing_details": details, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"status": True, "message": "Billing details saved.", "data": details}


@router.get("/invoices")
async def list_invoices(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
):
    cursor = (
        invoices_collection()
        .find({"user_id": user["_id"]})
        .sort("issued_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    return {
        "status": True,
        "count": len(docs),
        "data": [invoices.serialize(d) for d in docs],
    }


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, user: dict = Depends(get_current_user)):
    """One invoice, by id or by invoice number."""
    query: dict = {"user_id": user["_id"]}
    try:
        query["_id"] = ObjectId(invoice_id)
    except (InvalidId, TypeError):
        # Not an ObjectId, so treat it as the human-facing number: INV-0001
        # works in a URL just as well as the raw id.
        query["invoice_number"] = invoice_id

    doc = await invoices_collection().find_one(query)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found."
        )
    return {"status": True, "data": invoices.serialize(doc)}


@router.put("/auto-recharge")
async def set_auto_recharge(
    payload: AutoRechargeRequest, user: dict = Depends(get_current_user)
):
    """Store the low-balance top-up preference.

    The settings are recorded and reported, but nothing triggers on them yet:
    charging a customer unattended needs a saved payment method held by the
    gateway, which this service does not have. `GET /billing` reports exactly
    what was stored, so the dashboard never claims a recharge will happen.
    """
    await credits.get_account(user["_id"])
    settings_doc = {
        "enabled": payload.enabled,
        "threshold_credits": payload.threshold_credits,
        "amount_inr": payload.amount_inr,
    }
    await credit_accounts_collection().update_one(
        {"user_id": user["_id"]},
        {"$set": {"auto_recharge": settings_doc, "updated_at": datetime.now(timezone.utc)}},
    )
    account = await credits.get_account(user["_id"])
    return {
        "status": True,
        "message": "Auto-recharge settings saved. Automatic charging is not active yet.",
        "data": _account_view(account, user.get("plan")),
    }


@router.post("/top-up", status_code=status.HTTP_201_CREATED)
async def create_top_up(payload: TopUpRequest, user: dict = Depends(get_current_user)):
    """Create a pending top-up for the payment gateway to collect.

    No credits are granted here. The order is what the frontend hands to the
    gateway; credits appear only when the gateway confirms payment.
    """
    now = datetime.now(timezone.utc)

    if payload.plan is not None:
        key = payload.plan.lower().strip()
        if key not in PURCHASABLE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Plan '{payload.plan}' cannot be purchased. Valid: {', '.join(PURCHASABLE)}",
            )
        plan = get_plan(key)
        amount = plan.price_inr
        credit_units = credits.to_units(plan.credits_granted)
        plan_after = plan.key
        description = f"{plan.label} pack"
    else:
        # Custom amount: 1 credit per rupee, no bonus, no tier change.
        amount = money.to_decimal(payload.amount_inr)
        credit_units = credits.to_units(amount)
        plan_after = None
        description = "Credit top-up"

    settings = get_settings()
    # Rounded to whole paise: a gateway can only charge integer minor units, so
    # an amount it cannot charge exactly must not be the amount we record.
    amount = money.to_chargeable(amount)

    document = {
        "user_id": user["_id"],
        "amount_inr": money.to_bson(amount),
        "currency": settings.currency,
        "credit_units": credit_units,
        "plan": plan_after,
        "description": description,
        "status": "pending",
        # Handed to the gateway as its idempotency/reference key.
        "reference": f"cb_top_{secrets.token_urlsafe(12)}",
        "created_at": now,
    }
    result = await payments_collection().insert_one(document)
    document["_id"] = result.inserted_id

    # Create the matching gateway order, if one is configured. Done
    # after the local record exists so a gateway outage leaves a pending
    # top-up to retry rather than a charge with nothing pointing at it.
    checkout = None
    try:
        order = await payments_gateway.create_order(
            amount, document["currency"], document["reference"]
        )
    except payments_gateway.PaymentGatewayError as exc:
        logger.error("gateway order failed for payment %s: %s", document["_id"], exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment gateway is unavailable. The top-up was not started.",
        )

    if order is not None:
        await payments_collection().update_one(
            {"_id": document["_id"]}, {"$set": {"provider_order_id": order["id"]}}
        )
        document["provider_order_id"] = order["id"]
        # Everything the checkout widget needs on the client. The key id is
        # public by design; the secret never leaves this service.
        checkout = {
            "provider": "razorpay",
            "key_id": settings.razorpay_key_id,
            "order_id": order["id"],
            "amount": order["amount"],  # paise, as the gateway expects
            "currency": order["currency"],
        }

    return {
        "status": True,
        "message": "Top-up created. Complete payment to receive credits.",
        "data": {**_payment_view(document), "reference": document["reference"]},
        "checkout": checkout,
    }


@router.post("/payments/{payment_id}/confirm")
async def confirm_payment(
    payment_id: str,
    payload: PaymentConfirmation,
    x_billing_secret: str | None = Header(default=None, alias="X-Billing-Secret"),
):
    """Gateway webhook: mark a top-up paid and grant its credits.

    Guarded by a shared secret rather than a user session, because the caller
    is the payment provider, not the customer. Fails closed when no secret is
    configured — an unset secret must not mean "anyone may grant credits".
    """
    settings = get_settings()
    if not settings.billing_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing webhook is not configured (BILLING_WEBHOOK_SECRET unset).",
        )
    # Constant-time compare: a plain `!=` leaks the secret one character at a
    # time to anyone who can measure the response.
    if not x_billing_secret or not secrets.compare_digest(
        x_billing_secret, settings.billing_webhook_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid billing secret."
        )

    try:
        oid = ObjectId(payment_id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found."
        )

    payment = await payments_collection().find_one({"_id": oid})
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found."
        )

    return await _settle(
        oid,
        provider_payment_id=payload.provider_payment_id,
        method=payload.method,
        provider_invoice_id=payload.provider_invoice_id,
        provider_invoice_url=payload.provider_invoice_url,
    )


def _amount_text(value) -> str:
    """A money figure the way a receipt shows it: grouped, two decimals."""
    return f"{money.to_json(value):,.2f}"


# What the gateway calls a payment method, and what a customer calls it. The
# gateway sends lowercase machine values; `.title()` alone turns "upi" into
# "Upi", which is not a word anyone has ever written on a receipt.
_METHOD_NAMES = {
    "upi": "UPI",
    "card": "Card",
    "netbanking": "Net banking",
    "wallet": "Wallet",
    "emi": "EMI",
    "paylater": "Pay later",
    "bank_transfer": "Bank transfer",
}


def _method_text(method: str | None) -> str:
    if not method:
        return "Online payment"
    key = method.strip().lower()
    # A method we do not have a name for is shown as the gateway sent it,
    # rather than mangled — "Visa •••• 4242" must survive intact.
    return _METHOD_NAMES.get(key, method.strip())


async def _send_deposit_receipt(payment: dict, owner: dict, entry: dict) -> None:
    """Email the customer their top-up receipt.

    Best effort by design: the money has moved and the credits are granted, so
    nothing here may raise back into the settlement path. `send_email` already
    swallows delivery failures; this guards against the rest.
    """
    settings = get_settings()
    try:
        await email.send_deposit_receipt(
            owner.get("email", ""),
            owner.get("name"),
            amount=_amount_text(payment.get("amount_inr", 0)),
            balance=_amount_text(credits.from_units(int(entry["balance_after_units"]))),
            # The gateway's payment id is what support will be asked about; the
            # local reference is the fallback for a settlement that carried none.
            transaction_id=payment.get("provider_payment_id") or payment.get("reference", ""),
            method=_method_text(payment.get("method")),
            # The gateway reports a method ("upi", "card"), not a brand, and
            # inventing one would put a wrong logo on a receipt.
            provider="",
            paid_at=payment.get("paid_at") or datetime.now(timezone.utc),
            transaction_url=settings.dashboard_url_for,
        )
    except Exception as exc:
        logger.error("could not send deposit receipt for %s: %s", payment["_id"], exc)


async def _settle(
    oid: ObjectId,
    *,
    provider_payment_id: str | None,
    method: str | None = None,
    provider_invoice_id: str | None = None,
    provider_invoice_url: str | None = None,
) -> dict:
    """Mark a payment paid, grant its credits, upgrade the plan, invoice it.

    The single settlement path. Every route that can confirm a payment — the
    shared-secret endpoint, the checkout callback and the gateway
    webhook — funnels here **after** it has verified the caller, so there is
    one place where credits are granted and one place to reason about
    double-crediting.
    """
    # Gateways redeliver webhooks, and a customer can hit the callback while
    # the webhook is in flight. Claim the order with a conditional update so
    # only the first arrival credits the account; the rest read as replays.
    try:
        claimed = await payments_collection().find_one_and_update(
            {"_id": oid, "status": "pending"},
            {
                "$set": {
                    "status": "paid",
                    "provider_payment_id": provider_payment_id,
                    "method": method,
                    "provider_invoice_id": provider_invoice_id,
                    "provider_invoice_url": provider_invoice_url,
                    "paid_at": datetime.now(timezone.utc),
                }
            },
            return_document=True,
        )
    except DuplicateKeyError:
        # `provider_payment_id` is uniquely indexed, so this is one gateway
        # payment being used to settle a second order. Refusing is the whole
        # point of that index — never credit the same money twice.
        logger.error(
            "gateway payment %s already settled another order; refusing %s",
            provider_payment_id,
            oid,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That gateway payment has already been applied to another order.",
        )
    if claimed is None:
        current = await payments_collection().find_one({"_id": oid})
        return {
            "status": True,
            "replayed": True,
            "message": f"Payment already {current.get('status')}.",
            "data": _payment_view(current),
        }

    entry = await credits.grant(
        claimed["user_id"],
        int(claimed["credit_units"]),
        credits.KIND_PURCHASE,
        claimed.get("description", "Credit top-up"),
        ref=claimed["_id"],
    )

    # A pack purchase also moves the account onto that tier's limits.
    if claimed.get("plan"):
        await users_collection().update_one(
            {"_id": claimed["user_id"]},
            {"$set": {"plan": claimed["plan"], "updated_at": datetime.now(timezone.utc)}},
        )

    # Issue the invoice last. It runs inside the claimed branch, so a
    # redelivered webhook never produces a second one, and it cannot fail the
    # confirmation — the money has already moved.
    owner = await users_collection().find_one({"_id": claimed["user_id"]})
    invoice = await invoices.issue_for_payment(claimed, owner or {})
    if invoice is not None:
        await payments_collection().update_one(
            {"_id": claimed["_id"]},
            {"$set": {"invoice_number": invoice["invoice_number"]}},
        )
        claimed["invoice_number"] = invoice["invoice_number"]

    # Receipt to the customer. Inside the claimed branch, so a redelivered
    # webhook does not send a second one, and awaited rather than backgrounded
    # because the caller here is a payment gateway, not a browser waiting on a
    # page — `send_email` swallows its own failures either way.
    await _send_deposit_receipt(claimed, owner or {}, entry)

    return {
        "status": True,
        "replayed": False,
        "message": "Payment confirmed and credits added.",
        "data": _payment_view(claimed),
        "invoice": invoices.serialize(invoice) if invoice else None,
    }


@router.post("/payments/{payment_id}/verify")
async def verify_checkout(
    payment_id: str,
    payload: CheckoutCallback,
    user: dict = Depends(get_current_user),
):
    """Confirm a top-up from the checkout callback.

    The three values come back through the customer's browser, so they are
    untrusted: the HMAC over `order_id|payment_id` is the only thing that makes
    them believable. The order id must also match the one *this* payment was
    created with, or a valid signature from someone else's order would settle
    this one.
    """
    try:
        oid = ObjectId(payment_id)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found."
        )

    # Scoped to the caller: a customer may only settle their own top-up.
    payment = await payments_collection().find_one(
        {"_id": oid, "user_id": user["_id"]}
    )
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found."
        )

    if payment.get("provider_order_id") != payload.razorpay_order_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This order does not belong to that payment.",
        )
    if not payments_gateway.verify_checkout_signature(
        payload.razorpay_order_id,
        payload.razorpay_payment_id,
        payload.razorpay_signature,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment signature is not valid.",
        )

    return await _settle(oid, provider_payment_id=payload.razorpay_payment_id)


@router.post("/webhook/razorpay")
async def payment_webhook(request: Request):
    """The gateway webhook — the authoritative confirmation.

    Verified against `RAZORPAY_WEBHOOK_SECRET` over the **raw** body; a
    re-serialised payload hashes differently and every delivery would be
    rejected. This is what settles a payment when the customer closes the
    browser before the callback fires.
    """
    settings = get_settings()
    if not settings.razorpay_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment webhook is not configured (RAZORPAY_WEBHOOK_SECRET unset).",
        )

    raw = await request.body()
    if not payments_gateway.verify_webhook_signature(
        raw, request.headers.get("X-Razorpay-Signature", "")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed webhook body."
        )

    entity = (
        event.get("payload", {}).get("payment", {}).get("entity", {})
    )
    order_id = entity.get("order_id")
    if event.get("event") not in ("payment.captured", "payment.authorized"):
        # Acknowledge anything else so the gateway stops retrying — an
        # unrecognised event is not a failure on our side.
        return {"status": True, "ignored": event.get("event")}
    if not order_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook carried no order id."
        )

    payment = await payments_collection().find_one({"provider_order_id": order_id})
    if payment is None:
        # 200, not 404: an order we do not recognise is not something the gateway
        # can fix by retrying, and a 4xx would have it retry for days.
        logger.warning("gateway webhook for unknown order %s", order_id)
        return {"status": True, "ignored": "unknown_order"}

    return await _settle(
        payment["_id"],
        provider_payment_id=entity.get("id"),
        method=entity.get("method"),
    )
