"""Razorpay — order creation and signature verification.

The only module that talks to the payment gateway, the way `database.py` is the
only one that talks to Mongo and `email.py` the only one that sends mail.

Two ways a payment gets confirmed, and **both must be verified cryptographically
before a single credit is granted**:

* **Checkout callback** — the browser returns `razorpay_payment_id`,
  `razorpay_order_id` and `razorpay_signature`. The signature is
  ``HMAC_SHA256(order_id|payment_id, key_secret)``. It comes from the customer's
  browser, so it is untrusted input; the HMAC is the only thing that makes it
  believable.
* **Webhook** — Razorpay POSTs the event with `X-Razorpay-Signature`, which is
  ``HMAC_SHA256(raw_body, webhook_secret)``. Note **webhook_secret**, a separate
  value from the key secret — signing with the wrong one rejects every callback.

The webhook is the authority: a customer can close the browser before the
callback fires, but Razorpay will still deliver the webhook. The callback exists
so the dashboard can show credits immediately rather than waiting.

Uses the stdlib over HTTP rather than the Razorpay SDK — it is one POST and two
HMACs, and this keeps the deployed dependency set unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from decimal import Decimal

from starlette.concurrency import run_in_threadpool

from . import money
from .config import get_settings

logger = logging.getLogger("chatbucket_b2b.payments")

_ORDERS_URL = "https://api.razorpay.com/v1/orders"


class PaymentGatewayError(RuntimeError):
    """Razorpay could not be reached, or refused the order."""


def _create_order_blocking(amount_paise: int, currency: str, receipt: str) -> dict:
    settings = get_settings()
    payload = json.dumps(
        {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            # Razorpay retries an order with the same receipt rather than
            # creating a duplicate, which makes a retried top-up safe.
            "payment_capture": 1,
        }
    ).encode("utf-8")

    token = base64.b64encode(
        f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()
    ).decode()
    request = urllib.request.Request(
        _ORDERS_URL,
        data=payload,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=settings.razorpay_timeout_seconds
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise PaymentGatewayError(f"Razorpay rejected the order ({exc.code}): {detail}")
    except Exception as exc:
        raise PaymentGatewayError(f"Could not reach Razorpay: {exc}")


async def create_order(amount: Decimal, currency: str, receipt: str) -> dict | None:
    """Create a Razorpay order. Returns None when Razorpay is not configured.

    Returning None rather than raising keeps the service usable without a
    gateway — the top-up is recorded locally and can be confirmed by the
    shared-secret endpoint, which is how it worked before this existed.
    """
    settings = get_settings()
    if not settings.razorpay_configured:
        return None
    return await run_in_threadpool(
        _create_order_blocking, money.to_paise(amount), currency, receipt
    )


def verify_checkout_signature(
    order_id: str, payment_id: str, signature: str
) -> bool:
    """Verify the signature Checkout hands back to the browser."""
    settings = get_settings()
    if not settings.razorpay_key_secret:
        return False
    expected = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Verify a webhook delivery against the **webhook** secret.

    Signed over the exact bytes received, so the raw body must be used — a
    re-serialised JSON body produces a different digest and every delivery
    would be rejected.
    """
    settings = get_settings()
    if not settings.razorpay_webhook_secret:
        return False
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")
