"""Outbound email — the single place that sends mail.

Mirrors the seam `database.py` provides for Mongo: nothing else in the app
constructs a message or talks to a mail server, so the provider can change
without touching a router.

Transport is SMTP, which SendGrid, SES, Mailgun, Postmark and Gmail all speak,
so switching provider is a config change rather than a code change and costs no
extra dependency. `smtplib` is blocking, so the send runs in the threadpool for
the same reason password hashing does (see `security.py`).

Backends, chosen with ``EMAIL_BACKEND``:

* ``smtp``     — really send;
* ``console``  — log the message (local development);
* ``memory``   — append to `outbox`, for tests to assert against;
* ``disabled`` — drop silently, for deployments that intentionally send none.

**Sending never raises into a request.** A mail server being down must not turn
a successful password reset into a 500 — the failure is logged and reported
through the return value instead.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from starlette.concurrency import run_in_threadpool

from .config import get_settings

logger = logging.getLogger("chatbucket_b2b.email")

# Populated only by the `memory` backend. Tests read this; nothing else should.
outbox: list[dict] = []


def _build_message(to: str, subject: str, body: str) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    sender = settings.email_from
    if settings.email_from_name:
        sender = f"{settings.email_from_name} <{settings.email_from}>"
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def _send_smtp_blocking(message: EmailMessage) -> None:
    """Deliver over SMTP. Blocking — always call via `run_in_threadpool`."""
    settings = get_settings()
    context = ssl.create_default_context()

    if settings.smtp_use_ssl:
        server = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
            context=context,
        )
    else:
        server = smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
        )

    with server:
        if settings.smtp_starttls and not settings.smtp_use_ssl:
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)


async def send_email(to: str, subject: str, body: str) -> bool:
    """Send one message. Returns True if it was handed off successfully.

    Never raises: callers are request handlers and background tasks where a
    mail outage must not surface as a failed operation.
    """
    settings = get_settings()
    backend = settings.resolved_email_backend

    if backend == "disabled":
        logger.debug("email backend disabled; dropping message to %s", to)
        return False

    if not to:
        logger.warning("refusing to send %r with no recipient", subject)
        return False

    if backend == "memory":
        outbox.append({"to": to, "subject": subject, "body": body})
        return True

    if backend == "console":
        logger.warning(
            "EMAIL (console backend, not delivered)\nTo: %s\nSubject: %s\n\n%s",
            to,
            subject,
            body,
        )
        return True

    try:
        await run_in_threadpool(_send_smtp_blocking, _build_message(to, subject, body))
        logger.info("sent %r to %s", subject, to)
        return True
    except Exception as exc:
        # Includes auth failures, DNS/connect errors and timeouts. Logged with
        # the subject and recipient so a delivery gap is diagnosable.
        logger.error("failed to send %r to %s: %s", subject, to, exc)
        return False


# --- Messages --------------------------------------------------------------

async def send_password_reset(to: str, token: str) -> bool:
    """Email a password-reset link.

    Called as a background task so the response is sent first: doing it inline
    would make `forgot-password` measurably slower for a registered address
    than an unregistered one, reintroducing the account-enumeration oracle the
    endpoint's identical responses exist to close.
    """
    settings = get_settings()
    link = f"{settings.password_reset_url_for}?token={token}"
    minutes = settings.reset_token_expire_minutes
    # Deliberately ASCII-only (no em dashes): a non-ASCII body makes
    # `set_content` pick 8bit transfer encoding, which SMTP servers that do not
    # advertise 8BITMIME can reject outright. Plain ASCII stays quoted-printable
    # and delivers everywhere.
    body = (
        "Hi,\n\n"
        "We received a request to reset your ChatBucket password. Open the "
        "link below to choose a new one:\n\n"
        f"{link}\n\n"
        f"The link expires in {minutes} minutes and can be used once.\n\n"
        "If you didn't request this, you can ignore this email - your password "
        "will not change.\n\n"
        "-- ChatBucket\n"
    )
    return await send_email(to, "Reset your ChatBucket password", body)


async def send_email_verification(to: str, token: str) -> bool:
    """Email a link confirming the address belongs to whoever signed up."""
    settings = get_settings()
    link = f"{settings.email_verify_url_for}?token={token}"
    hours = settings.verification_token_expire_hours
    # ASCII-only, for the same 8BITMIME reason as the reset email above.
    body = (
        "Hi,\n\n"
        "Confirm this email address to finish setting up your ChatBucket "
        "account:\n\n"
        f"{link}\n\n"
        f"The link expires in {hours} hours.\n\n"
        "If you did not create an account, you can ignore this email.\n\n"
        "-- ChatBucket\n"
    )
    return await send_email(to, "Confirm your ChatBucket email", body)


async def send_demo_request_notification(lead: dict) -> bool:
    """Tell sales about a new demo request.

    No-op when `SALES_EMAIL` is unset, so the endpoint still records the lead.
    """
    settings = get_settings()
    if not settings.sales_email:
        logger.info("SALES_EMAIL not set; demo request %s not notified", lead.get("_id"))
        return False

    lines = [
        f"New {lead.get('type', 'unknown')} demo request.",
        "",
        f"Name:    {lead.get('name', '')}",
        f"Email:   {lead.get('email', '')}",
        f"Mobile:  {lead.get('mobile', '')}",
    ]
    if lead.get("company_name"):
        lines.append(f"Company: {lead['company_name']}")
    if lead.get("company_details"):
        lines += ["", "Company details:", lead["company_details"]]
    if lead.get("how_did_you_hear"):
        lines += ["", "How they heard about us:", lead["how_did_you_hear"]]
    lines += [
        "",
        f"Wants product updates: {'yes' if lead.get('subscribe_updates') else 'no'}",
        f"Lead id: {lead.get('_id', '')}",
    ]

    subject = f"New demo request: {lead.get('name', 'unknown')}"
    if lead.get("company_name"):
        subject += f" ({lead['company_name']})"
    return await send_email(settings.sales_email, subject, "\n".join(lines))
