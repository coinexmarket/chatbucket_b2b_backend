"""Service status — backs the API Status page.

    GET  /status                  the page: every system, plus 90 days of history
    GET  /status/{service}        one system
    POST /status/heartbeat        a service reports in      (X-Status-Secret)
    PUT  /status/{service}        set by hand for incidents (X-Status-Secret)

`GET /status` is public: a status page nobody can read during an outage is not
much of a status page.

Writes are guarded by a shared secret rather than a user session, because the
callers are the AI services and operators, not customers. Without the secret
configured, nothing can write — anyone able to set "operational" could hide a
real outage from every customer at once.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi import status as http

from .. import status as service_status
from ..config import get_settings
from ..database import service_status_collection
from ..models.status import HeartbeatRequest, StatusUpdateRequest
from ..serialization import iso

router = APIRouter(prefix="/status", tags=["status"])

_HEADLINE = {
    service_status.OPERATIONAL: "All systems operational",
    service_status.DEGRADED: "Some systems are degraded",
    service_status.DOWN: "We are experiencing an outage",
    service_status.MAINTENANCE: "Undergoing scheduled maintenance",
    service_status.UNKNOWN: "System status is currently unknown",
}


def _require_secret(provided: str | None) -> None:
    settings = get_settings()
    if not settings.status_webhook_secret:
        raise HTTPException(
            status_code=http.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Status reporting is not configured (STATUS_WEBHOOK_SECRET unset).",
        )
    # Constant-time: a plain `!=` leaks the secret a character at a time.
    if not provided or not secrets.compare_digest(
        provided, settings.status_webhook_secret
    ):
        raise HTTPException(
            status_code=http.HTTP_401_UNAUTHORIZED, detail="Invalid status secret."
        )


@router.get("")
async def get_status(days: int = Query(default=90, ge=1, le=365)):
    """Every system's current status, with daily history for the uptime strip."""
    docs = {
        doc["service"]: doc
        for doc in await service_status_collection().find({}).to_list(length=None)
    }
    now = datetime.now(timezone.utc)

    services = []
    for system in service_status.SYSTEMS.values():
        doc = docs.get(system.key)
        current = service_status.apply_staleness(doc, now)
        reported = doc.get("reported_at") if doc else None
        services.append({
            "service": system.key,
            "name": system.name,
            "components": system.components,
            "status": current,
            "detail": doc.get("detail") if doc else None,
            "source": doc.get("source") if doc else None,
            "last_reported_at": iso(reported)
            if reported
            else None,
            "history": await service_status.history(system.key, days, now),
        })

    overall = service_status.worst([s["status"] for s in services])
    return {
        "status": True,
        "overall": overall,
        "message": _HEADLINE[overall],
        "checked_at": now.isoformat(),
        "data": services,
    }


@router.get("/{service}")
async def get_service_status(
    service: str, days: int = Query(default=90, ge=1, le=365)
):
    try:
        system = service_status.get_system(service)
    except service_status.UnknownSystemError as exc:
        raise HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail=str(exc))

    doc = await service_status_collection().find_one({"service": system.key})
    now = datetime.now(timezone.utc)
    reported = doc.get("reported_at") if doc else None
    return {
        "status": True,
        "data": {
            "service": system.key,
            "name": system.name,
            "components": system.components,
            "status": service_status.apply_staleness(doc, now),
            "detail": doc.get("detail") if doc else None,
            "source": doc.get("source") if doc else None,
            "last_reported_at": iso(reported)
            if reported
            else None,
            "history": await service_status.history(system.key, days, now),
        },
    }


@router.post("/heartbeat")
async def heartbeat(
    payload: HeartbeatRequest,
    x_status_secret: str | None = Header(default=None, alias="X-Status-Secret"),
):
    """A service reporting that it is alive.

    Send this on a schedule shorter than `STATUS_STALE_AFTER_SECONDS`; stop,
    and the service reads `unknown` rather than staying green.
    """
    _require_secret(x_status_secret)
    try:
        record = await service_status.record(
            payload.service,
            payload.status,
            service_status.SOURCE_HEARTBEAT,
            payload.detail,
        )
    except (service_status.UnknownSystemError, ValueError) as exc:
        raise HTTPException(status_code=http.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"status": True, "data": {**record, "reported_at": record["reported_at"].isoformat()}}


@router.put("/{service}")
async def set_status(
    service: str,
    payload: StatusUpdateRequest,
    x_status_secret: str | None = Header(default=None, alias="X-Status-Secret"),
):
    """Set a status by hand, for an incident or scheduled maintenance.

    A manual status does not go stale — it stands until changed, so declaring
    an outage is not quietly undone by the staleness rule.
    """
    _require_secret(x_status_secret)
    try:
        record = await service_status.record(
            service, payload.status, service_status.SOURCE_MANUAL, payload.detail
        )
    except (service_status.UnknownSystemError, ValueError) as exc:
        raise HTTPException(status_code=http.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"status": True, "data": {**record, "reported_at": record["reported_at"].isoformat()}}
