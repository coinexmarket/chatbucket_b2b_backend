"""Operator-triggered email — announcements, maintenance notices and the runs.

    POST /notifications/announcement            press note / product news
    POST /notifications/maintenance             a maintenance window
    POST /notifications/monthly-reports         usage report for a month
    POST /notifications/onboarding-nudges       registered, never called the API
    POST /notifications/free-credit-reminders   trial window closing

Six of the designed emails have no customer action that produces them. These
endpoints are where a person or a scheduler starts them; `notifications.py`
holds the rules about who gets what and how often.

**Gated by `OPS_SECRET`, not a user session**, on the same principle as the
engine-burn view: the caller is an operator or a cron job, and nothing a
customer can authenticate as should be able to mail the entire customer base.
Unset means the endpoints return 503 rather than falling open.

Every broadcast takes either `testEmail` — send one copy to that address,
record nothing — or `confirm: true`. There is no way to reach real customers by
leaving a field out.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi import status as http

from .. import email, notifications, reports
from ..config import get_settings
from ..models.notifications import (
    AnnouncementRequest,
    MaintenanceRequest,
    MonthlyReportRequest,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _require_ops_secret(provided: str | None) -> None:
    settings = get_settings()
    if not settings.ops_secret:
        raise HTTPException(
            status_code=http.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notification sending is not configured (OPS_SECRET unset).",
        )
    # Constant-time: a plain `!=` leaks the secret a character at a time.
    if not provided or not secrets.compare_digest(provided, settings.ops_secret):
        raise HTTPException(
            status_code=http.HTTP_401_UNAUTHORIZED, detail="Invalid ops secret."
        )


def _preview(delivered: bool, reference_id: str | None = None) -> dict:
    return {
        "status": True,
        "preview": True,
        "message": "Sent one copy to the test address. Nothing was recorded.",
        "reference_id": reference_id,
        "delivered": delivered,
    }


@router.post("/announcement")
async def announce(
    payload: AnnouncementRequest,
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
):
    """Send an announcement to the customer base, or preview it."""
    _require_ops_secret(x_ops_secret)
    announcement = notifications.build_announcement(
        subject=payload.subject,
        headline=payload.headline,
        summary=payload.summary,
        highlights=payload.highlights,
        quote=payload.quote,
        quote_author=payload.quote_author,
        category=payload.category,
        hero_title=payload.hero_title,
        hero_subtitle=payload.hero_subtitle,
        reference_id=payload.reference_id,
    )

    if payload.test_email:
        delivered = await email.send_announcement(payload.test_email, announcement)
        return _preview(delivered, announcement["reference_id"])

    result = await notifications.broadcast_announcement(
        announcement, verified_only=payload.verified_only
    )
    return {
        "status": True,
        "reference_id": announcement["reference_id"],
        "data": result.as_dict(),
    }


@router.post("/maintenance")
async def maintenance(
    payload: MaintenanceRequest,
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
):
    """Tell customers about a maintenance window, or preview the notice."""
    _require_ops_secret(x_ops_secret)
    window = notifications.build_maintenance_window(
        subject=payload.subject,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        maintenance_type=payload.maintenance_type,
        reference_id=payload.reference_id,
    )

    if payload.test_email:
        delivered = await email.send_maintenance_notice(
            payload.test_email, "there", window
        )
        return _preview(delivered, window["reference_id"])

    result = await notifications.broadcast_maintenance(
        window, verified_only=payload.verified_only
    )
    return {
        "status": True,
        "reference_id": window["reference_id"],
        "data": result.as_dict(),
    }


@router.post("/monthly-reports")
async def monthly_reports(
    payload: MonthlyReportRequest,
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
):
    """Send every account with usage its report for a month.

    Safe to re-run: each account is recorded once per month, so a second run
    fills in whoever the first one failed to reach and skips the rest.
    """
    _require_ops_secret(x_ops_secret)
    month = payload.month
    if month is None:
        # The month that has just ended — what a run on the 1st means.
        this_month, _ = reports.month_window(datetime.now(timezone.utc))
        month, _ = reports.previous_month_window(this_month)
    elif month.tzinfo is None:
        month = month.replace(tzinfo=timezone.utc)

    begin, _ = reports.month_window(month)
    result = await notifications.send_monthly_reports(begin)
    return {"status": True, "month": begin.strftime("%Y-%m"), "data": result.as_dict()}


@router.post("/onboarding-nudges")
async def onboarding_nudges(
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
):
    """Nudge accounts that registered a while ago and never called the API.

    Sent once per account, ever — being reminded weekly that you have not used
    something is not a nudge, it is a nag.
    """
    _require_ops_secret(x_ops_secret)
    result = await notifications.send_onboarding_nudges()
    return {"status": True, "data": result.as_dict()}


@router.post("/verification-reminders")
async def verification_reminders(
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
):
    """Chase accounts that registered but never confirmed their address.

    Mints a fresh code and link for each — the pair issued at signup expired
    within minutes, so resending the original would hand them a dead code.
    Sent once per account, ever.
    """
    _require_ops_secret(x_ops_secret)
    result = await notifications.send_verification_reminders()
    return {"status": True, "data": result.as_dict()}


@router.post("/free-credit-reminders")
async def free_credit_reminders(
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
):
    """Remind accounts whose trial-credit window is nearly up."""
    _require_ops_secret(x_ops_secret)
    result = await notifications.send_free_credit_reminders()
    return {"status": True, "data": result.as_dict()}
