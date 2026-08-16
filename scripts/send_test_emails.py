"""Render — or really send — one sample of every designed email.

Two ways to look at these. Writing them to disk is instant, needs nothing
configured, and is the right check for layout and copy. Really sending them is
the only check for what a mail client does with the layout, which is a different
question and the one that actually catches problems: Outlook's table handling,
Gmail clipping a long message, an image the CDN will not serve over TLS.

    python -m scripts.send_test_emails --list
    python -m scripts.send_test_emails --out ./preview          # write HTML
    python -m scripts.send_test_emails --to you@example.com     # really send
    python -m scripts.send_test_emails --to you@example.com --only welcome,deposit

Sending uses whatever `SMTP_*` configuration is in the environment, exactly as
the running app would; there is no separate path and no separate credentials.
With none set the app resolves to the `console` backend and nothing leaves the
machine — the script says so rather than pretending it sent.

The sample data is deliberately awkward where real data will be: markup
characters in the free-text slots an operator types into (escaping), a
five-figure amount (grouping), a metric that fell rather than grew (the red
down-arrow branch the design does not ship), and an account with no trial
credits (the panel that should disappear rather than read zero). A sample that
only shows the happy path only tests the happy path.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import email, notifications
from app.config import get_settings

# Only the first word reaches the greeting (see `email._first_name`), so a
# surname is not where escaping gets tested — the announcement copy below is.
NAME = "Asha O'Brien"
NOW = datetime(2026, 8, 16, 18, 59, tzinfo=timezone.utc)

# The monthly report is the one message whose context is too large to invent at
# the call site. These figures mirror what `reports.build_monthly_report`
# produces, including a metric that fell — the design only ships the up arrow,
# and the down arrow is the branch worth looking at.
_REPORT = {
    "period": "01 July 2026 - 31 July 2026",
    "previous_period": "Jun 2026",
    "generated_on": "01 August 2026",
    "plan_name": "Pro",
    "plan_status": "Active",
    "analytics_url": "https://app.chatbucket.business/dashboard",
    "upgrade_url": "https://app.chatbucket.business/dashboard",
    "headline_note": "Your usage grew against last month.",
    "headline_cheer": "Great job! 🚀",
    "services": [
        {"name": "Translation", "percent": "42", "value": "₹4,148.00", "color": "#5421C7", "bar_height": 59},
        {"name": "Chat Agent", "percent": "28", "value": "₹2,760.00", "color": "#7C4DEE", "bar_height": 39},
        {"name": "Speech to Text", "percent": "17", "value": "₹1,678.00", "color": "#A07BF5", "bar_height": 24},
        {"name": "Text to Speech", "percent": "8", "value": "₹790.00", "color": "#C4AAFA", "bar_height": 11},
        {"name": "Voice Agent (call)", "percent": "5", "value": "₹499.00", "color": "#E2D6FD", "bar_height": 7},
    ],
    "metric1_label": "Total Requests", "metric1_value": "12,450", "metric1_previous": "10,500",
    "metric1_change": "18.6%", "metric1_arrow": "↑", "metric1_color": "#239653", "metric1_background": "#DDF5E6",
    "metric2_label": "Total Spend", "metric2_value": "₹9,875.00", "metric2_previous": "₹8,069.00",
    "metric2_change": "22.4%", "metric2_arrow": "↑", "metric2_color": "#239653", "metric2_background": "#DDF5E6",
    # Deliberately down: the red/down branch of every growth pill.
    "metric3_label": "Voice Minutes Used", "metric3_value": "4,320", "metric3_previous": "4,716",
    "metric3_change": "8.4%", "metric3_arrow": "↓", "metric3_color": "#C2334D", "metric3_background": "#FBE4E9",
    "metric4_label": "Agent Interactions", "metric4_value": "6,780", "metric4_previous": "5,573",
    "metric4_change": "21.7%", "metric4_arrow": "↑", "metric4_color": "#239653", "metric4_background": "#DDF5E6",
    "bar1_label": "Translation", "bar1_percent": "42", "bar1_amount": "₹4,148.00 / ₹9,875.00",
    "bar2_label": "Chat Agent", "bar2_percent": "28", "bar2_amount": "₹2,760.00 / ₹9,875.00",
    "bar3_label": "Speech to Text", "bar3_percent": "17", "bar3_amount": "₹1,678.00 / ₹9,875.00",
    "insight1_title": "You're Growing!", "insight1_text": "Your spend is up 22.4% on last month. Keep going!",
    "insight2_title": "Automation at work", "insight2_text": "Agents handled 6,780 interactions this month.",
    "insight3_title": "Pro Tip", "insight3_text": "Translation is your biggest line. Batch those calls to cut cost.",
    "has_usage": True,
}


def _samples(to: str) -> dict:
    """One coroutine factory per message, keyed by the name `--only` takes."""
    return {
        "welcome": lambda: email.send_welcome(to, NAME, "1,000"),
        # The same email for a deployment that grants no trial credits. The
        # free-credits panel should be gone, not showing zero.
        "welcome-no-bonus": lambda: email.send_welcome(to, None, None),
        "verification": lambda: email.send_email_verification(
            to, "3f9a2c7e-sample-verification-token", "048213", NAME
        ),
        # Sent once the address is confirmed — the moment the account
        # stops being read-only and the signup credits become spendable.
        "email-verified": lambda: email.send_email_verified(to, NAME, "100"),
        "password-reset": lambda: email.send_password_reset(
            to, "8b1d4f60-sample-reset-token", NAME
        ),
        "contact-received": lambda: email.send_contact_received({
            "_id": "68a0f1c2b3d4e5f600112233",
            "name": NAME,
            "email": to,
            "created_at": NOW,
        }),
        "subscribed": lambda: email.send_subscription_confirmation(to),
        "deposit": lambda: email.send_deposit_receipt(
            to, NAME,
            amount="5,000.00", balance="6,250.00", transaction_id="pay_ROq1x8ZkLm42",
            method="UPI", provider="", paid_at=NOW,
            transaction_url=f"{get_settings().dashboard_url_for}/billing",
        ),
        "withdrawal": lambda: email.send_withdrawal_request(
            to, NAME,
            amount="5,000.00", status="Processing", eta="1-3 Business Days",
            transaction_id="wd_88213004", method="Bank transfer", provider="",
            requested_at=NOW,
            transaction_url=f"{get_settings().dashboard_url_for}/billing",
        ),
        "free-credits": lambda: email.send_free_credits_expiring(
            to, NAME, days_remaining=7, expires_at=NOW + timedelta(days=7)
        ),
        "onboarding-nudge": lambda: email.send_onboarding_nudge(to, NAME),
        "monthly-report": lambda: email.send_monthly_report(to, NAME, _REPORT),
        # An announcement is free text an operator types, so it is where an
        # unescaped `<`, `&` or quote would actually reach a customer's inbox.
        # These read as punctuation if the escaping is right, and as broken
        # markup (or a missing paragraph) if it is not.
        "announcement": lambda: email.send_announcement(to, notifications.build_announcement(
            subject="[TEST] ChatBucket raises its Series A",
            headline='ChatBucket raises its Series A — "the next chapter" <2026>',
            summary="We've closed a round to take AI communication to every business "
                    "& every language. Read on for what changes.",
            highlights=[
                "200+ languages, live",
                "Voice agents built in <2 minutes",
                'SOC 2 Type II underway & "DPDP-ready"',
            ],
            quote="This is the beginning, not the destination.",
            quote_author="Founder / CEO, ChatBucket",
            category="Company news",
            reference_id="ANN-SAMPLE",
            when=NOW,
        )),
        "maintenance": lambda: email.send_maintenance_notice(to, NAME,
            notifications.build_maintenance_window(
                subject="[TEST] Scheduled maintenance on 20 August",
                starts_at=NOW + timedelta(days=4),
                ends_at=NOW + timedelta(days=4, hours=2),
                reference_id="MNT-SAMPLE",
            )),
    }


def _write(out: Path, sent: list[dict]) -> None:
    """Write each message to its own file, plus an index linking them."""
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, message in sent:
        body = message.get("html")
        if body is None:
            # The template failed to render; the text part is what would ship.
            body = f"<pre>{message['body']}</pre>"
            note = " (HTML FAILED TO RENDER — text part shown)"
        else:
            note = f" ({len(body):,} bytes)"
        (out / f"{name}.html").write_text(body, encoding="utf-8")
        rows.append(
            f'<li><a href="{name}.html">{name}</a> — '
            f'<code>{message["subject"]}</code>{note}</li>'
        )

    (out / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'>"
        "<title>ChatBucket email previews</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:40px;max-width:900px}"
        "li{margin:6px 0;line-height:1.6}code{color:#555}</style>"
        "<h1>ChatBucket email previews</h1>"
        f"<p>{len(rows)} messages, rendered by the real send functions.</p>"
        f"<ul>{''.join(rows)}</ul>",
        encoding="utf-8",
    )
    print(f"\nWrote {len(sent)} previews to {out.resolve()}")
    print(f"Open {(out / 'index.html').resolve()}")


def _use_backend(name: str) -> None:
    """Switch the mail backend for the next pass. Settings are cached."""
    os.environ["EMAIL_BACKEND"] = name
    get_settings.cache_clear()


async def _render(names: list[str], to: str) -> tuple[list[tuple[str, dict]], int]:
    """Render every sample through the memory backend and check each one.

    Always runs, including before a real send: a message with a hole in it
    should be caught here rather than in somebody's inbox.
    """
    samples = _samples(to)
    email.outbox.clear()

    problems = 0
    collected: list[tuple[str, dict]] = []
    for name in names:
        await samples[name]()
        message = email.outbox[-1]
        collected.append((name, message))

        if message.get("html") is None:
            print(f"FAILED  {name:18} template did not render (see the log above)")
            problems += 1
        elif "{{" in message["html"]:
            print(f"FAILED  {name:18} unrendered placeholders left in the HTML")
            problems += 1
        else:
            print(f"ok      {name:18} {len(message['html']):>7,} bytes  {message['subject']}")
    return collected, problems


async def _send(names: list[str], to: str) -> int:
    """Really hand each message to the mail server."""
    samples = _samples(to)
    failed = 0
    for name in names:
        delivered = await samples[name]()
        print(f"{'sent  ' if delivered else 'FAILED'}  {name}")
        failed += 0 if delivered else 1
    return failed


def main() -> int:
    # `email.py` reports a failed render or a refused connection through the
    # log and nothing else, so a script that swallows the log reports "FAILED"
    # with no reason attached.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--to", help="Address to send to. Omit to render without sending.")
    parser.add_argument("--out", help="Directory to write the rendered HTML into.")
    parser.add_argument("--only", help="Comma-separated subset; see --list.")
    parser.add_argument("--list", action="store_true", help="List the sample names and exit.")
    args = parser.parse_args()

    available = list(_samples("preview@example.com"))
    if args.list:
        print("\n".join(available))
        return 0

    names = available
    if args.only:
        names = [n.strip() for n in args.only.split(",") if n.strip()]
        unknown = [n for n in names if n not in available]
        if unknown:
            parser.error(f"unknown sample(s): {', '.join(unknown)}. See --list.")

    # What the app *would* use, read before the render pass overrides it.
    configured = get_settings().resolved_email_backend
    recipient = args.to or "preview@example.com"

    if args.to and configured != "smtp":
        print(
            f"EMAIL_BACKEND resolves to '{configured}', so nothing can be delivered to "
            f"{args.to}.\nSet SMTP_HOST (and credentials) to really send, or drop "
            "--to to write the HTML out instead.",
            file=sys.stderr,
        )
        return 2

    # Render first, always. The SMTP backend keeps no copy, so this is the only
    # pass that can check the markup — and a broken message is better caught
    # here than in somebody's inbox.
    _use_backend("memory")
    print(f"Rendering {len(names)} messages...\n")
    collected, problems = asyncio.run(_render(names, recipient))

    if args.out or not args.to:
        _write(Path(args.out or "email-preview"), collected)

    if problems:
        print(f"\n{problems} problem(s); not sending.", file=sys.stderr)
        return 1

    if not args.to:
        return 0

    _use_backend(configured)
    settings = get_settings()
    print(
        f"\nSending to {args.to} via {settings.smtp_host}:{settings.smtp_port} "
        f"as {settings.email_from}\n"
    )
    failed = asyncio.run(_send(names, args.to))
    if failed:
        print(
            f"\n{failed} message(s) were not accepted by the mail server. "
            "The reason is logged under `chatbucket_b2b.email`.",
            file=sys.stderr,
        )
        return 1
    print(f"\nAll {len(names)} sent. Check {args.to}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
