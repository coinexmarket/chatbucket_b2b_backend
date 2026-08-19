"""Lifecycle email jobs — the sends that are not triggered by a request.

Six of the designed templates have no single moment in a request cycle that
produces them: a monthly report is due when a month ends, a credit reminder
when a window is closing, an announcement when someone decides to make one.
This module is where those runs live, so the routers stay about requests and
`email.py` stays about messages.

Two rules apply to every job here, because the failure modes of bulk mail are
worse than the failure modes of one-off mail:

**Send once.** Each send claims a row in `notifications` first, under a unique
index on (user, kind, key). A job that is retried, run twice by an overlapping
schedule, or re-run by hand skips whoever already has the row. The claim is
taken *before* the send and released again if the send fails, so a mail outage
does not permanently mark a customer as notified.

**Send slowly.** Recipients are worked through in bounded batches
(`BROADCAST_CONCURRENCY`), not fanned out at once. Opening one SMTP connection
per customer in parallel is the quickest route to a throttled sending domain,
and a report run over a large base would do exactly that.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from . import credits, email, emailtemplates, reports, verification
from .config import get_settings
from .database import (
    credit_ledger_collection,
    job_runs_collection,
    notifications_collection,
    usage_collection,
    users_collection,
)
from .logsafe import log_safe

logger = logging.getLogger("chatbucket_b2b.notifications")

# `kind` values in the notifications collection.
KIND_MONTHLY_REPORT = "monthly_report"
KIND_ONBOARDING_NUDGE = "onboarding_nudge"
KIND_VERIFICATION_REMINDER = "verification_reminder"
KIND_FREE_CREDITS_EXPIRING = "free_credits_expiring"
KIND_ANNOUNCEMENT = "announcement"
KIND_MAINTENANCE = "maintenance"


@dataclass
class RunResult:
    """What a job did, in the shape an operator needs to read it."""

    sent: int = 0
    skipped: int = 0
    failed: int = 0
    considered: int = 0
    truncated: bool = False

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            # True means the run stopped at BROADCAST_MAX_RECIPIENTS with more
            # to do. Reported rather than logged, because a half-finished
            # broadcast that looks finished is how people get mailed twice.
            "truncated": self.truncated,
        }


# --- Send-once bookkeeping -------------------------------------------------

async def _claim(user_id, kind: str, key: str) -> bool:
    """Reserve one send. False means somebody already has it."""
    try:
        await notifications_collection().insert_one({
            "user_id": user_id,
            "kind": kind,
            "key": key,
            "sent_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


async def _release(user_id, kind: str, key: str) -> None:
    """Give the claim back after a failed send, so a retry can take it."""
    await notifications_collection().delete_one(
        {"user_id": user_id, "kind": kind, "key": key}
    )


async def _send_once(user_id, kind: str, key: str, send) -> str:
    """Claim, send, and unclaim on failure. Returns sent | skipped | failed."""
    if not await _claim(user_id, kind, key):
        return "skipped"
    try:
        delivered = await send()
    except Exception as exc:
        logger.error("%s to %s raised: %s", kind, user_id, exc)
        delivered = False
    if not delivered:
        await _release(user_id, kind, key)
        return "failed"
    return "sent"


async def _run_batched(items, handler) -> RunResult:
    """Work through `items` `BROADCAST_CONCURRENCY` at a time."""
    settings = get_settings()
    result = RunResult(considered=len(items))
    size = settings.broadcast_concurrency

    for start in range(0, len(items), size):
        batch = items[start : start + size]
        outcomes = await asyncio.gather(
            *(handler(item) for item in batch), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                logger.error("notification batch item failed: %s", outcome)
                result.failed += 1
            elif outcome == "sent":
                result.sent += 1
            elif outcome == "skipped":
                result.skipped += 1
            else:
                result.failed += 1
    return result


async def _recipients(*, verified_only: bool, extra_query: dict | None = None) -> tuple[list[dict], bool]:
    """Accounts to mail, capped at `BROADCAST_MAX_RECIPIENTS`.

    Returns the list and whether the cap cut it short. `verified_only` is the
    safer default for anything promotional: an address nobody has confirmed is
    as likely to be a typo of a stranger's inbox as it is to be the customer's.
    """
    settings = get_settings()
    # A closed account keeps its row (invoices point at it) with the address
    # replaced by a `@deleted.invalid` placeholder. Mailing those is a bounce
    # per closed account, every broadcast, straight into the sender reputation.
    query: dict = {"deleted_at": {"$exists": False}}
    query.update(extra_query or {})
    if verified_only:
        query["email_verified"] = True

    limit = settings.broadcast_max_recipients
    # One over the cap, so "exactly at the cap" is distinguishable from "more
    # than the cap" without a second count query.
    docs = await users_collection().find(
        query, {"email": 1, "name": 1, "plan": 1, "created_at": 1}
    ).limit(limit + 1).to_list(length=limit + 1)

    if len(docs) > limit:
        return docs[:limit], True
    return docs, False


# --- Broadcasts ------------------------------------------------------------

def build_announcement(
    *,
    subject: str,
    headline: str,
    summary: str,
    highlights: list[str],
    quote: str = "",
    quote_author: str = "",
    category: str = "Announcement",
    hero_title: str | None = None,
    hero_subtitle: str | None = None,
    reference_id: str | None = None,
    when: datetime | None = None,
) -> dict:
    """The context one announcement is rendered from, built once per broadcast.

    The reference id is generated here rather than per recipient, so everyone
    who was told the same thing can quote the same id back at support.
    """
    moment = when or datetime.now(timezone.utc)
    return {
        "subject": subject,
        "headline": headline,
        "summary": summary,
        "highlights": list(highlights),
        "quote": quote,
        "quote_author": quote_author,
        "category": category,
        "hero_title": hero_title or headline,
        "hero_subtitle": hero_subtitle or summary,
        "reference_id": reference_id or f"ANN-{secrets.token_hex(4).upper()}",
        "date": emailtemplates.fmt_date(moment),
        "time": emailtemplates.fmt_time(moment),
    }


def build_maintenance_window(
    *,
    subject: str,
    starts_at: datetime,
    ends_at: datetime,
    maintenance_type: str = "Scheduled Maintenance",
    reference_id: str | None = None,
) -> dict:
    return {
        "subject": subject,
        "maintenance_type": maintenance_type,
        "reference_id": reference_id or f"MNT-{secrets.token_hex(4).upper()}",
        "start_date": emailtemplates.fmt_short_date(starts_at),
        "start_time": emailtemplates.fmt_time(starts_at),
        "end_date": emailtemplates.fmt_short_date(ends_at),
        "end_time": emailtemplates.fmt_time(ends_at),
    }


async def broadcast_announcement(
    announcement: dict, *, verified_only: bool = True
) -> RunResult:
    """Send one announcement to the customer base."""
    people, truncated = await _recipients(verified_only=verified_only)

    async def handle(person: dict) -> str:
        return await _send_once(
            person["_id"],
            KIND_ANNOUNCEMENT,
            announcement["reference_id"],
            lambda: email.send_announcement(person.get("email", ""), announcement),
        )

    result = await _run_batched(people, handle)
    result.truncated = truncated
    logger.info("announcement %s: %s", log_safe(announcement["reference_id"]), result.as_dict())
    return result


async def broadcast_maintenance(window: dict, *, verified_only: bool = False) -> RunResult:
    """Tell the base about a maintenance window.

    Unverified addresses are included by default here, unlike an announcement:
    this is service information for anyone who might call the API during the
    window, not marketing.
    """
    people, truncated = await _recipients(verified_only=verified_only)

    async def handle(person: dict) -> str:
        return await _send_once(
            person["_id"],
            KIND_MAINTENANCE,
            window["reference_id"],
            lambda: email.send_maintenance_notice(
                person.get("email", ""), person.get("name"), window
            ),
        )

    result = await _run_batched(people, handle)
    result.truncated = truncated
    logger.info("maintenance %s: %s", log_safe(window["reference_id"]), result.as_dict())
    return result


# --- Scheduled runs --------------------------------------------------------

async def send_monthly_reports(month_start: datetime | None = None) -> RunResult:
    """Send every account its report for a month.

    Defaults to the month that has just ended, which is what a run on the 1st
    wants; pass `month_start` to re-send an older one.
    """
    if month_start is None:
        this_month, _ = reports.month_window(datetime.now(timezone.utc))
        month_start, _ = reports.previous_month_window(this_month)
    begin, _ = reports.month_window(month_start)
    key = begin.strftime("%Y-%m")

    people, truncated = await _recipients(verified_only=False)

    async def handle(person: dict) -> str:
        report = await reports.build_monthly_report(person, begin)
        if not report["has_usage"]:
            # A report of zeroes is not information, and an account that did
            # not use the service in a month did not opt into a monthly email
            # about not using the service.
            return "skipped"
        return await _send_once(
            person["_id"],
            KIND_MONTHLY_REPORT,
            key,
            lambda: email.send_monthly_report(
                person.get("email", ""), person.get("name"), report
            ),
        )

    result = await _run_batched(people, handle)
    result.truncated = truncated
    logger.info("monthly reports for %s: %s", key, result.as_dict())
    return result


async def send_onboarding_nudges(now: datetime | None = None) -> RunResult:
    """Nudge accounts that registered a while ago and never called the API."""
    settings = get_settings()
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=settings.onboarding_nudge_after_days)

    people, truncated = await _recipients(
        verified_only=False, extra_query={"created_at": {"$lte": cutoff}}
    )

    async def handle(person: dict) -> str:
        # One record is enough to disqualify them; `find_one` stops there
        # rather than counting a busy account's whole history.
        used = await usage_collection().find_one({"user_id": person["_id"]}, {"_id": 1})
        if used is not None:
            return "skipped"
        return await _send_once(
            person["_id"],
            KIND_ONBOARDING_NUDGE,
            "once",
            lambda: email.send_onboarding_nudge(person.get("email", ""), person.get("name")),
        )

    result = await _run_batched(people, handle)
    result.truncated = truncated
    logger.info("onboarding nudges: %s", result.as_dict())
    return result


async def send_verification_reminders(now: datetime | None = None) -> RunResult:
    """Chase accounts that registered but never confirmed their address.

    With `REQUIRE_EMAIL_VERIFICATION` on, an unverified account is a stuck
    account: it can sign in, it has been granted its signup credits, and it can
    do nothing with them. Nothing else in the system chases those, so without
    this they sit blocked until they think to ask.

    A **fresh** code and link are minted. The pair issued at signup is long
    dead by now — the code lasts ten minutes — and re-sending a dead credential
    is worse than sending nothing, because the customer tries it and concludes
    the product is broken.

    Sent once per account, ever. Somebody who has decided not to verify does
    not need reminding weekly.
    """
    settings = get_settings()
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(hours=settings.verification_reminder_after_hours)

    people, truncated = await _recipients(
        verified_only=False,
        extra_query={
            "created_at": {"$lte": cutoff},
            "email_verified": {"$ne": True},
        },
    )

    async def handle(person: dict) -> str:
        async def send() -> bool:
            token, code = await verification.issue_credentials(person["_id"])
            return await email.send_email_verification(
                person.get("email", ""), token, code, person.get("name")
            )

        return await _send_once(person["_id"], KIND_VERIFICATION_REMINDER, "once", send)

    result = await _run_batched(people, handle)
    result.truncated = truncated
    logger.info("verification reminders: %s", result.as_dict())
    return result


async def send_free_credit_reminders(now: datetime | None = None) -> RunResult:
    """Remind accounts whose signup bonus is nearing the end of its window.

    Nothing here expires credits — the product line is that credits do not
    expire, and this job does not change that. What it counts down is the
    window the welcome email promised (`FREE_CREDIT_VALIDITY_DAYS`), measured
    from when the bonus was granted, and it only writes to anyone who still has
    a balance to spend.
    """
    settings = get_settings()
    moment = now or datetime.now(timezone.utc)
    validity = timedelta(days=settings.free_credit_validity_days)
    # Granted early enough that the window closes within the reminder period,
    # but not so long ago that it has already closed.
    newest = moment - (validity - timedelta(days=settings.free_credit_reminder_days))
    oldest = moment - validity

    grants = await credit_ledger_collection().find({
        "kind": credits.KIND_SIGNUP_BONUS,
        "created_at": {"$gt": oldest, "$lte": newest},
    }).limit(settings.broadcast_max_recipients).to_list(
        length=settings.broadcast_max_recipients
    )

    async def handle(grant: dict) -> str:
        user = await users_collection().find_one(
            {"_id": grant["user_id"], "deleted_at": {"$exists": False}}
        )
        if user is None:
            return "skipped"
        granted_at = grant["created_at"]
        if granted_at.tzinfo is None:
            granted_at = granted_at.replace(tzinfo=timezone.utc)
        expires_at = granted_at + validity
        # Rounded up: with 30 hours left, "expires in 1 day" is the honest
        # reading and "in 2 days" is not.
        days_remaining = max((expires_at - moment + timedelta(hours=23)).days, 1)

        if await credits.balance_units(user["_id"]) <= 0:
            # Nothing left to spend, so nothing to be reminded about.
            return "skipped"

        return await _send_once(
            user["_id"],
            KIND_FREE_CREDITS_EXPIRING,
            expires_at.strftime("%Y-%m-%d"),
            lambda: email.send_free_credits_expiring(
                user.get("email", ""),
                user.get("name"),
                days_remaining=days_remaining,
                expires_at=expires_at,
            ),
        )

    result = await _run_batched(grants, handle)
    logger.info("free credit reminders: %s", result.as_dict())
    return result


# --- Scheduling ------------------------------------------------------------
# The three recurring jobs run on a timer inside the app rather than from an
# external cron, because this service deploys as a single component and adding
# a scheduled-job component to run three curls is more moving parts than the
# problem deserves.
#
# Two things make that safe. The app runs several workers, each with its own
# copy of this loop, so every run is claimed in `job_runs` under a unique
# (job, period) index — one worker wins, the rest skip. And the claim is
# released if the run raises, so a transient failure is retried on the next
# tick rather than marking the period done for good.

JOB_MONTHLY_REPORT = "monthly_reports"
JOB_FREE_CREDIT_REMINDERS = "free_credit_reminders"
JOB_ONBOARDING_NUDGES = "onboarding_nudges"
JOB_VERIFICATION_REMINDERS = "verification_reminders"

# How long after the configured day the monthly report will still go out. It
# exists so an outage on the 1st does not silently skip a month — and it is
# bounded so that *enabling* the scheduler late in the month does not fire a
# surprise retroactive send to the whole customer base.
_MONTHLY_CATCHUP_DAYS = 7


async def _claim_run(job: str, period: str) -> bool:
    """Reserve one job for one period. False means another worker has it."""
    try:
        await job_runs_collection().insert_one({
            "job": job,
            "period": period,
            "started_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


async def _finish_run(job: str, period: str, result: RunResult) -> None:
    await job_runs_collection().update_one(
        {"job": job, "period": period},
        {"$set": {"finished_at": datetime.now(timezone.utc), "result": result.as_dict()}},
    )


async def _release_run(job: str, period: str) -> None:
    """Hand the period back so the next tick retries it."""
    await job_runs_collection().delete_one({"job": job, "period": period})


async def _run_claimed(job: str, period: str, run) -> RunResult | None:
    """Claim, run, record. None means somebody else had it, or it failed."""
    if not await _claim_run(job, period):
        return None
    try:
        result = await run()
    except Exception as exc:
        # Released rather than left claimed: a Mongo blip during the monthly
        # run must not mean nobody gets a report this month.
        logger.error("scheduled job %s (%s) failed, will retry: %s", job, period, exc)
        await _release_run(job, period)
        return None
    await _finish_run(job, period, result)
    logger.info("scheduled job %s (%s): %s", job, period, result.as_dict())
    return result


def _due_jobs(local: datetime) -> list[tuple[str, str]]:
    """The (job, period) pairs eligible right now, in the display timezone.

    Eligibility is a pure function of the clock; whether a job has *already*
    run for its period is the claim's business, not this function's.
    """
    settings = get_settings()
    if local.hour < settings.notification_scheduler_hour:
        return []

    today = local.strftime("%Y-%m-%d")
    due = [
        (JOB_FREE_CREDIT_REMINDERS, today),
        (JOB_ONBOARDING_NUDGES, today),
        (JOB_VERIFICATION_REMINDERS, today),
    ]

    # The monthly report's period is the month it *covers*, not the month it is
    # sent in, so claiming it on the 1st cannot be re-claimed on the 2nd.
    start_day = settings.notification_monthly_report_day
    if start_day <= local.day < start_day + _MONTHLY_CATCHUP_DAYS:
        covered = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        due.append((JOB_MONTHLY_REPORT, covered))
    return due


async def run_due_jobs(now: datetime | None = None) -> dict:
    """Run whatever is due and not already claimed. Safe to call any time."""
    local = now or emailtemplates.local_now()
    runners = {
        JOB_FREE_CREDIT_REMINDERS: lambda _p: send_free_credit_reminders(),
        JOB_ONBOARDING_NUDGES: lambda _p: send_onboarding_nudges(),
        JOB_VERIFICATION_REMINDERS: lambda _p: send_verification_reminders(),
        # The period *is* the month to report on, so a catch-up run on the 3rd
        # still reports the right month rather than whatever "last month" means
        # at the moment it happens to execute.
        JOB_MONTHLY_REPORT: lambda p: send_monthly_reports(
            datetime.strptime(p, "%Y-%m").replace(tzinfo=timezone.utc)
        ),
    }

    ran: dict = {}
    for job, period in _due_jobs(local):
        result = await _run_claimed(job, period, lambda j=job, p=period: runners[j](p))
        if result is not None:
            ran[job] = {"period": period, **result.as_dict()}
    return ran


async def scheduler_loop() -> None:
    """Wake periodically and run anything due. Started from the app lifespan.

    Never exits on error: a scheduler that dies on one bad tick is worse than
    no scheduler, because it looks like one.
    """
    settings = get_settings()
    interval = settings.notification_scheduler_interval_seconds
    logger.info(
        "notification scheduler on: checking every %ds, daily jobs at %02d:00 %s, "
        "monthly report on day %d",
        interval,
        settings.notification_scheduler_hour,
        settings.display_timezone,
        settings.notification_monthly_report_day,
    )
    while True:
        # Sleeps first, so a restart loop cannot hammer the jobs and so a
        # deploy does not fire mail the instant it boots.
        await asyncio.sleep(interval)
        try:
            await run_due_jobs()
        except Exception as exc:
            logger.error("notification scheduler tick failed: %s", exc)
