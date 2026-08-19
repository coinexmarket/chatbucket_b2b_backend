"""Outbound SMS — the single place that sends a text message.

The same seam `email.py` is for mail: nothing else in the app talks to a
gateway, so the gateway can change without touching a router. Deliberately
vendor-neutral — the request is built from `SMS_API_URL`, `SMS_API_KEY`,
`SMS_SENDER_ID` and `SMS_TEMPLATE_ID`, so swapping provider is configuration.

Backends, chosen with ``SMS_BACKEND``:

* ``http``     — really send;
* ``console``  — log the message (local development);
* ``memory``   — append to `outbox`, for tests to assert against;
* ``disabled`` — drop silently.

**Sending never raises into a request**, for the same reason mail does not: a
gateway outage must not turn a successful signup into a 500.

Two India-specific facts shape this module. TRAI requires the sender header and
the message template to be **pre-registered on a DLT platform**; an
unregistered pair is refused by the operator rather than the gateway, which
presents as silent non-delivery with a 200 response. And an SMS costs money per
message, which is why `routers/auth.py` rate-limits requests for one far more
tightly than it does anything free.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request

from starlette.concurrency import run_in_threadpool

from .config import get_settings

logger = logging.getLogger("chatbucket_b2b.sms")

# Populated only by the `memory` backend. Tests read this; nothing else should.
outbox: list[dict] = []


# Words that mean the gateway refused the message. It answers **200 with an
# error in the body** rather than an HTTP error code, so the status alone cannot
# be trusted — checking only that would report every rejection as a success and
# leave nobody to notice that no OTP ever arrives.
_REFUSAL_MARKERS = (
    "invalid", "error", "fail", "insufficient", "unauthor", "denied",
    "not found", "missing", "blocked", "expire",
)


def local_number(phone: str) -> str:
    """E.164 to the digits the gateway expects.

    Numbers are stored `+919876543210`; the gateway's own examples use bare
    ten-digit numbers. Dropping the country code is therefore the default, but
    it is a property of the gateway rather than of the number, so
    `SMS_STRIP_COUNTRY_CODE` can turn it off for one that wants `91…`.
    """
    settings = get_settings()
    digits = phone.lstrip("+")
    if not settings.sms_strip_country_code:
        return digits
    for code in settings.sms_country_code_list:
        bare = code.lstrip("+")
        if digits.startswith(bare):
            return digits[len(bare):]
    return digits


def _deliver_blocking(to: str, body: str) -> tuple[bool, str]:
    """Hand one message to the gateway. Blocking — call via threadpool.

    **This is the one function to adapt to a different gateway.** Everything
    else in this module, and every caller, is gateway-agnostic.

    A GET with query parameters, which is what this class of gateway speaks.
    `urlencode` does the escaping: the message contains spaces, an apostrophe
    and a full stop, and hand-built query strings are how those turn into a
    mangled template that no longer matches the DLT registration.

    The stdlib is used rather than adding an HTTP client dependency, for the
    same reason `main.py`'s status prober does.
    """
    settings = get_settings()
    query = urllib.parse.urlencode({
        "username": settings.sms_username,
        "apikey": settings.sms_api_key,
        "senderid": settings.sms_sender_id,
        "mobile": local_number(to),
        "message": body,
        "templateid": settings.sms_template_id,
    })
    url = f"{settings.sms_api_url}?{query}"

    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=settings.sms_timeout_seconds) as response:
        text = response.read(2000).decode("utf-8", errors="replace").strip()

    if not 200 <= response.status < 300:
        return False, text
    # A message id means accepted; anything reading like a complaint means it
    # was not. Accepted is still not delivered — the handset's verdict arrives
    # via a delivery-report callback this service does not consume.
    lowered = text.lower()
    if any(marker in lowered for marker in _REFUSAL_MARKERS) or not text:
        return False, text
    return True, text


async def send_sms(to: str, body: str) -> bool:
    """Send one message. Returns True if the gateway accepted it.

    Never raises. Callers are request handlers where a gateway outage must not
    fail the operation that triggered the message.
    """
    settings = get_settings()
    backend = settings.resolved_sms_backend

    if backend == "disabled":
        logger.debug("sms backend disabled; dropping message to %s", to)
        return False

    if not to:
        logger.warning("refusing to send an SMS with no recipient")
        return False

    if backend == "memory":
        outbox.append({"to": to, "body": body})
        return True

    if backend == "console":
        logger.warning(
            "SMS (console backend, not delivered)\nTo: %s\n\n%s", to, body
        )
        return True

    if not settings.sms_api_url:
        logger.error("SMS_BACKEND=http but SMS_API_URL is unset; dropping message")
        return False

    try:
        accepted, detail = await run_in_threadpool(_deliver_blocking, to, body)
    except urllib.error.HTTPError as exc:
        # The gateway's own body usually says why — an unregistered template, a
        # spent balance — and that is the whole diagnosis, so it is logged.
        logger.error(
            "sms to %s refused: HTTP %s %s", to, exc.code,
            exc.read(500).decode("utf-8", errors="replace"),
        )
        return False
    except Exception as exc:
        logger.error("sms to %s failed: %s", to, exc)
        return False

    if not accepted:
        logger.error("sms to %s not accepted by the gateway: %s", to, detail[:300])
        return False

    # The message id is the only handle the gateway can trace a message by,
    # so it is logged rather than discarded.
    logger.info("sent verification SMS to %s (gateway ref: %s)", to, detail)
    return True


# --- Messages --------------------------------------------------------------

# The DLT-registered template, verbatim, with `{#var#}` where the platform
# expects a variable. **Do not reword this to read better.** The operator
# matches the delivered text against the registered template and silently drops
# anything that differs — including a changed apostrophe or a fixed typo. If the
# registration is ever updated, update this string in the same change.
#
# Two variables are registered: the code, and a trailing one. What the second is
# for is a property of the registration rather than of this service, so it is
# configurable (`SMS_TEMPLATE_SUFFIX`) instead of guessed at here.
_OTP_TEMPLATE = (
    "ChatBucket: {code} is your OTP for secure access to ChatBucket. "
    "It's valid for one attempt only. Don't share this OTP is confidential. {suffix}"
)


def render_otp_message(code: str) -> str:
    """The OTP text, exactly as the registered template defines it."""
    settings = get_settings()
    return _OTP_TEMPLATE.format(code=code, suffix=settings.sms_template_suffix).strip()


async def send_phone_verification(to: str, code: str) -> bool:
    """Text the six-digit code that confirms a mobile number.

    The wording comes from `_OTP_TEMPLATE`, which mirrors the DLT registration.
    If they diverge, delivery stops with no error anywhere in this codebase —
    the gateway still answers 200 and the operator drops the message.
    """
    return await send_sms(to, render_otp_message(code))
