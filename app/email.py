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

Every customer-facing message goes out as **multipart/alternative**: the
designed HTML from `emailtemplates`, and a plain-text part written by hand next
to it. The text part is not a fallback nobody reads — it is what a screen
reader, a text-only client and every spam filter sees, so each one says the
same thing the HTML does, links included.

**Sending never raises into a request.** A mail server being down must not turn
a successful password reset into a 500 — the failure is logged and reported
through the return value instead. A *template* failure is treated the same way:
the text part still goes out (see `_html`).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

from starlette.concurrency import run_in_threadpool

from . import emailtemplates
from .config import get_settings

logger = logging.getLogger("chatbucket_b2b.email")

# Populated only by the `memory` backend. Tests read this; nothing else should.
outbox: list[dict] = []


def _build_message(
    to: str, subject: str, body: str, html: str | None, reply_to: str | None
) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    sender = settings.email_from
    if settings.email_from_name:
        sender = f"{settings.email_from_name} <{settings.email_from}>"
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    if reply_to:
        # `EMAIL_FROM` is a no-reply address. Anything a customer might answer
        # needs somewhere for the answer to land.
        message["Reply-To"] = reply_to

    # `cte` is set explicitly. Left to choose, Python picks 8bit for any body
    # containing a non-ASCII character — and these templates are full of them
    # (the rupee sign, an em dash, an emoji). An SMTP server that does not
    # advertise 8BITMIME may reject such a message outright; quoted-printable
    # is 7-bit clean and delivers everywhere.
    message.set_content(body, subtype="plain", charset="utf-8", cte="quoted-printable")
    if html:
        message.add_alternative(html, subtype="html", charset="utf-8", cte="quoted-printable")
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
        if settings.smtp_debug:
            # Prints the whole SMTP conversation, including the provider's
            # queue id on the final 250. That id is the only handle support at
            # the provider can trace a message by, and without it "we sent it"
            # is an assertion rather than evidence. Never leave this on in
            # production: the dialogue includes the base64 AUTH line.
            server.set_debuglevel(1)
        if settings.smtp_starttls and not settings.smtp_use_ssl:
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)

        # `send_message` only raises when *every* recipient is refused. With a
        # single recipient the two cases coincide, but a partial refusal on a
        # multi-recipient message would otherwise return quietly and be logged
        # as a success. Never discard this.
        refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


async def send_email(
    to: str,
    subject: str,
    body: str,
    html: str | None = None,
    reply_to: str | None = None,
) -> bool:
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
        outbox.append({
            "to": to,
            "subject": subject,
            "body": body,
            "html": html,
            "reply_to": reply_to,
        })
        return True

    if backend == "console":
        # The HTML is not printed: it is tens of kilobytes of table markup and
        # would bury every other line in the log. Its presence is noted instead.
        logger.warning(
            "EMAIL (console backend, not delivered)\nTo: %s\nSubject: %s\nHTML: %s\n\n%s",
            to,
            subject,
            f"{len(html)} bytes" if html else "none",
            body,
        )
        return True

    try:
        await run_in_threadpool(
            _send_smtp_blocking, _build_message(to, subject, body, html, reply_to)
        )
        logger.info("sent %r to %s", subject, to)
        return True
    except Exception as exc:
        # Includes auth failures, DNS/connect errors and timeouts. Logged with
        # the subject and recipient so a delivery gap is diagnosable.
        logger.error("failed to send %r to %s: %s", subject, to, exc)
        return False


def _html(template: str, context: dict) -> str | None:
    """Render a template, or return None if it cannot be rendered.

    A missing context value is a bug in the caller, and one worth seeing in the
    log — but not one worth withholding a password-reset link over. The message
    then goes out as plain text alone, which every client can still read.
    """
    try:
        return emailtemplates.render(template, context)
    except Exception as exc:
        logger.error("could not render email template %r: %s", template, exc)
        return None


def _first_name(name: str | None) -> str:
    """The greeting each design opens with. Falls back to something neutral.

    Accounts predating the signup form have no name, and "Hi ," reads as a
    broken mail merge — which is exactly what it is.
    """
    first = (name or "").strip().split(" ")[0]
    return first or "there"


# --- Messages --------------------------------------------------------------

async def send_password_reset(to: str, token: str, name: str | None = None) -> bool:
    """Email a password-reset link.

    Called as a background task so the response is sent first: doing it inline
    would make `forgot-password` measurably slower for a registered address
    than an unregistered one, reintroducing the account-enumeration oracle the
    endpoint's identical responses exist to close.
    """
    settings = get_settings()
    link = f"{settings.password_reset_url_for}?token={token}"
    minutes = settings.reset_token_expire_minutes
    greeting = _first_name(name)
    body = (
        f"Hi {greeting},\n\n"
        "We received a request to reset your ChatBucket password. Open the "
        "link below to choose a new one:\n\n"
        f"{link}\n\n"
        f"The link expires in {minutes} minutes and can be used once.\n\n"
        "If you didn't request this, you can ignore this email - your password "
        "will not change.\n\n"
        "-- ChatBucket\n"
    )
    html = _html(
        "password_reset",
        {"name": greeting, "reset_url": link, "expiry_minutes": minutes},
    )
    return await send_email(to, "Reset your ChatBucket password", body, html)


async def send_email_verification(
    to: str, token: str, code: str, name: str | None = None
) -> bool:
    """Email the verification code, and the link that does the same job.

    Both are sent because they suit different situations: the code is what
    someone types back into a form they already have open, the link is what
    works from a phone reading mail in another app. Either one verifies the
    address; see `routers/auth.py`.
    """
    settings = get_settings()
    link = f"{settings.email_verify_url_for}?token={token}"
    minutes = settings.email_otp_expire_minutes
    hours = settings.verification_token_expire_hours
    body = (
        f"Hi {_first_name(name)},\n\n"
        f"Your ChatBucket verification code is {code}\n\n"
        f"It expires in {minutes} minutes. Do not share it with anyone.\n\n"
        "You can also confirm this address by opening:\n\n"
        f"{link}\n\n"
        f"That link expires in {hours} hours.\n\n"
        "If you did not create an account, you can ignore this email.\n\n"
        "-- ChatBucket\n"
    )
    context = {
        "otp": code,
        "expiry_minutes": minutes,
        "verify_url": link,
        # The design sets each digit in its own box.
        **{f"otp_{i + 1}": digit for i, digit in enumerate(code)},
    }
    html = _html("email_verification", context)
    return await send_email(to, f"{code} is your ChatBucket verification code", body, html)


async def send_email_verified(
    to: str, name: str | None, credits_available: str | None = None
) -> bool:
    """Confirm that an address has been verified and the account is unlocked.

    Sent after verification rather than instead of it: until this point the
    account could sign in but not create an API key, so "verified" is a real
    change in what the customer can do, not a formality.
    """
    settings = get_settings()
    greeting = _first_name(name)
    symbol = emailtemplates.currency_symbol()

    lines = [
        f"Hi {greeting},",
        "",
        "Your email address is verified and your ChatBucket account is now "
        "fully active. You can create an API key and start building.",
        "",
    ]
    if credits_available:
        lines += [f"You have {symbol}{credits_available} of credits ready to spend.", ""]
    lines += [
        f"Dashboard: {settings.dashboard_url_for}",
        "",
        "-- ChatBucket",
        "",
    ]
    html = _html(
        "email_verified",
        {"name": greeting, "credits": credits_available or "0"},
    )
    return await send_email(
        to,
        "Your ChatBucket email is verified",
        "\n".join(lines),
        html,
        reply_to=settings.support_email,
    )


async def send_welcome(
    to: str, name: str | None, bonus_credits: str | None = None
) -> bool:
    """Welcome a new account, and say what its trial balance is worth.

    `bonus_credits` is None when the deployment grants none, which hides the
    free-credits panel entirely rather than promising ₹0 of demo usage.
    """
    settings = get_settings()
    symbol = emailtemplates.currency_symbol()
    days = settings.free_credit_validity_days
    greeting = _first_name(name)

    lines = [
        f"Hi {greeting},",
        "",
        "Welcome to ChatBucket. Your account is ready.",
        "",
    ]
    if bonus_credits:
        lines += [
            f"We have added {symbol}{bonus_credits} of free credits to get you "
            f"started. They are good for {days} days.",
            "",
        ]
    lines += [
        f"Open your dashboard: {settings.dashboard_url_for}",
        "",
        f"Questions? Reply to this note or write to {settings.support_email}.",
        "",
        "-- ChatBucket",
        "",
    ]
    html = _html(
        "welcome",
        {
            "name": greeting,
            "bonus_credits": bonus_credits or "",
            "credit_validity_days": days,
        },
    )
    return await send_email(
        to, "Welcome to ChatBucket", "\n".join(lines), html, reply_to=settings.support_email
    )


async def send_contact_received(lead: dict) -> bool:
    """Acknowledge a demo request to the person who sent it.

    Separate from `send_demo_request_notification`, which tells sales. Both are
    queued off the same submission; this is the half the customer sees.
    """
    settings = get_settings()
    received = lead.get("created_at") or datetime.now(timezone.utc)
    query_id = str(lead.get("_id", ""))
    greeting = _first_name(lead.get("name"))
    email = lead.get("email", "")

    body = (
        f"Hi {greeting},\n\n"
        "Thanks for reaching out to ChatBucket. We have your message and "
        "someone will come back to you within 24 business hours.\n\n"
        f"Your query ID: {query_id}\n"
        f"Received: {emailtemplates.fmt_date(received)}, {emailtemplates.fmt_time(received)}\n"
        f"Submitted email: {email}\n\n"
        f"You can check on it any time at {settings.track_query_url_for}\n\n"
        "-- ChatBucket\n"
    )
    html = _html(
        "contact_received",
        {
            "name": greeting,
            "query_id": query_id,
            "received_date": emailtemplates.fmt_date(received),
            "received_time": emailtemplates.fmt_time(received),
            "submitted_email": email,
        },
    )
    return await send_email(
        email,
        "We've received your query",
        body,
        html,
        reply_to=settings.support_email,
    )


async def send_demo_request_notification(lead: dict) -> bool:
    """Tell sales about a new demo request.

    No-op when `SALES_EMAIL` is unset, so the endpoint still records the lead.
    Plain text on purpose: this one goes to a colleague, not a customer, and a
    designed template would only get in the way of pasting it into a CRM.
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
    return await send_email(
        settings.sales_email,
        subject,
        "\n".join(lines),
        reply_to=lead.get("email") or None,
    )


async def send_deposit_receipt(
    to: str,
    name: str | None,
    *,
    amount: str,
    balance: str,
    transaction_id: str,
    method: str,
    provider: str,
    paid_at: datetime,
    transaction_url: str,
) -> bool:
    """Confirm a completed top-up and state the new balance."""
    settings = get_settings()
    symbol = emailtemplates.currency_symbol()
    greeting = _first_name(name)
    date, time = emailtemplates.fmt_date(paid_at), emailtemplates.fmt_time(paid_at)

    body = "\n".join([
        f"Hi {greeting},",
        "",
        f"Your deposit of {symbol}{amount} was successful.",
        "",
        f"Amount added:   {symbol}{amount}",
        f"Payment method: {method} {provider}".rstrip(),
        f"Transaction ID: {transaction_id}",
        f"Date and time:  {date}, {time}",
        f"New balance:    {symbol}{balance}",
        "",
        f"See the details: {transaction_url}",
        "",
        "-- ChatBucket",
        "",
    ])
    html = _html(
        "deposit",
        {
            "name": greeting,
            "amount": amount,
            "balance": balance,
            "transaction_id": transaction_id,
            "payment_method": method,
            "payment_provider": provider,
            "date": date,
            "time": time,
            "transaction_url": transaction_url,
        },
    )
    return await send_email(
        to,
        f"Deposit successful: {symbol}{amount} added",
        body,
        html,
        reply_to=settings.support_email,
    )


async def send_withdrawal_request(
    to: str,
    name: str | None,
    *,
    amount: str,
    status: str,
    eta: str,
    transaction_id: str,
    method: str,
    provider: str,
    requested_at: datetime,
    transaction_url: str,
) -> bool:
    """Acknowledge a withdrawal request.

    Nothing calls this yet: this service has no payouts feature, and inventing
    one to give the template a caller would be worse than leaving the seam
    ready. Wire it into whatever creates the withdrawal record.
    """
    settings = get_settings()
    symbol = emailtemplates.currency_symbol()
    greeting = _first_name(name)
    date, time = (
        emailtemplates.fmt_date(requested_at),
        emailtemplates.fmt_time(requested_at),
    )

    body = "\n".join([
        f"Hi {greeting},",
        "",
        f"We have received your withdrawal request for {symbol}{amount} and it "
        "is being processed.",
        "",
        f"Amount:         {symbol}{amount}",
        f"Transfer to:    {method} {provider}".rstrip(),
        f"Transaction ID: {transaction_id}",
        f"Requested:      {date}, {time}",
        f"Status:         {status}",
        f"Expected:       {eta}",
        "",
        f"Track it: {transaction_url}",
        "",
        "-- ChatBucket",
        "",
    ])
    html = _html(
        "withdrawal",
        {
            "name": greeting,
            "amount": amount,
            "status": status,
            "eta": eta,
            "transaction_id": transaction_id,
            "transfer_method": method,
            "transfer_provider": provider,
            "date": date,
            "time": time,
            "transaction_url": transaction_url,
        },
    )
    return await send_email(
        to,
        f"Withdrawal request received: {symbol}{amount}",
        body,
        html,
        reply_to=settings.support_email,
    )


async def send_subscription_confirmation(to: str) -> bool:
    """Confirm a newsletter / app-launch subscription.

    The subscribe form collects an address and nothing else, so this template
    carries no name — which is why it greets the community rather than a person.
    """
    settings = get_settings()
    body = (
        "Thanks for subscribing to ChatBucket.\n\n"
        "We'll let you know about product updates, launches, offers and the "
        "occasional tip. No noise.\n\n"
        f"Dashboard: {settings.dashboard_url_for}\n\n"
        "-- ChatBucket\n"
    )
    html = _html("subscribed", {})
    return await send_email(to, "You're subscribed to ChatBucket", body, html)


async def send_free_credits_expiring(
    to: str, name: str | None, *, days_remaining: int, expires_at: datetime
) -> bool:
    """Remind an account to spend its trial credits before the window closes."""
    settings = get_settings()
    greeting = _first_name(name)
    date, time = (
        emailtemplates.fmt_date(expires_at),
        emailtemplates.fmt_time(expires_at),
    )
    day_word = "day" if days_remaining == 1 else "days"

    body = (
        f"Hi {greeting},\n\n"
        f"Your ChatBucket free credits expire in {days_remaining} {day_word} - "
        f"on {date}, {time}.\n\n"
        "Use them to try speech-to-text, text-to-speech, translation, chat "
        "agents and voice agents before they go.\n\n"
        f"Open your dashboard: {settings.dashboard_url_for}\n\n"
        "-- ChatBucket\n"
    )
    html = _html(
        "free_credits_expiring",
        {
            "name": greeting,
            "days_remaining": days_remaining,
            "expiry_date": date,
            "expiry_time": time,
        },
    )
    return await send_email(
        to,
        f"Your free credits expire in {days_remaining} {day_word}",
        body,
        html,
        reply_to=settings.support_email,
    )


async def send_onboarding_nudge(to: str, name: str | None) -> bool:
    """Nudge an account that registered but has never called the API."""
    settings = get_settings()
    greeting = _first_name(name)
    body = (
        f"Hi {greeting},\n\n"
        "You registered with ChatBucket but haven't tried the platform yet. "
        "Two things worth five minutes:\n\n"
        "  * a chatbot that books and reschedules appointments;\n"
        "  * a voice agent that answers calls and takes actions.\n\n"
        "Both start from a single prompt describing what you want.\n\n"
        f"Sign in: {settings.login_url_for}\n\n"
        f"Stuck? Write to {settings.support_email} and we'll set it up with you.\n\n"
        "-- ChatBucket\n"
    )
    html = _html(
        "onboarding_nudge",
        {
            "name": greeting,
            # All three CTAs land on the dashboard; the builder is one surface,
            # not three pages, so pointing them anywhere else would 404.
            "appointment_url": settings.dashboard_url_for,
            "voice_agent_url": settings.dashboard_url_for,
            "prompt_url": settings.dashboard_url_for,
        },
    )
    return await send_email(
        to,
        "Let's build something with ChatBucket",
        body,
        html,
        reply_to=settings.support_email,
    )


async def send_monthly_report(to: str, name: str | None, report: dict) -> bool:
    """Send one account its usage report for a month.

    `report` is built by `reports.build_monthly_report`, which is where the
    figures come from; this function only turns them into a message.
    """
    settings = get_settings()
    greeting = _first_name(name)

    lines = [
        f"Hi {greeting},",
        "",
        f"Your ChatBucket usage for {report['period']}:",
        "",
    ]
    for index in range(1, 5):
        lines.append(
            f"  {report[f'metric{index}_label']}: {report[f'metric{index}_value']}"
            f"  ({report[f'metric{index}_change']} vs {report['previous_period']}:"
            f" {report[f'metric{index}_previous']})"
        )
    if report["services"]:
        lines += ["", "Top services by spend:"]
        for service in report["services"]:
            lines.append(f"  {service['name']}: {service['percent']}% ({service['value']})")
    lines += [
        "",
        f"Plan: {report['plan_name']} ({report['plan_status']})",
        "",
        f"Full analytics: {report['analytics_url']}",
        "",
        "-- ChatBucket",
        "",
    ]

    html = _html("monthly_report", {"name": greeting, **report})
    return await send_email(
        to,
        f"Your ChatBucket usage report - {report['period']}",
        "\n".join(lines),
        html,
        reply_to=settings.support_email,
    )


async def send_announcement(to: str, announcement: dict) -> bool:
    """Send one announcement to one recipient.

    `announcement` carries the copy (headline, summary, highlights, quote) and
    the reference id; `notifications.broadcast_announcement` builds it once and
    fans it out.
    """
    settings = get_settings()
    lines = [
        announcement["headline"],
        "",
        announcement["summary"],
        "",
    ]
    for point in announcement["highlights"]:
        lines.append(f"  * {point}")
    if announcement["quote"]:
        lines += ["", f'"{announcement["quote"]}"', f"  - {announcement['quote_author']}"]
    lines += [
        "",
        f"{announcement['category']} | {announcement['date']} {announcement['time']}",
        f"Reference: {announcement['reference_id']}",
        "",
        f"More at {settings.marketing_url}",
        "",
        "-- ChatBucket",
        "",
    ]
    html = _html("announcement", announcement)
    return await send_email(to, announcement["subject"], "\n".join(lines), html)


async def send_maintenance_notice(to: str, name: str | None, window: dict) -> bool:
    """Tell one customer about a maintenance window."""
    settings = get_settings()
    greeting = _first_name(name)
    body = (
        f"Hi {greeting},\n\n"
        f"{window['maintenance_type']} is planned for ChatBucket.\n\n"
        f"Starts: {window['start_date']}, {window['start_time']}\n"
        f"Ends:   {window['end_date']}, {window['end_time']}\n"
        f"Reference: {window['reference_id']}\n\n"
        "Messaging, voice and video, translation, chat and voice agents, and "
        "analytics may be intermittently unavailable during the window. Your "
        "data is not affected.\n\n"
        f"Live status: {settings.track_query_url_for}\n\n"
        "-- ChatBucket\n"
    )
    html = _html("maintenance", {"name": greeting, **window})
    return await send_email(to, window["subject"], body, html)
