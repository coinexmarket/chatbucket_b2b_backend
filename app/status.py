"""Service status: the registry, and how a status is decided.

This backend cannot observe the AI services directly, so status has to be
*reported* to it. Three sources, all writing the same record:

* ``heartbeat`` — a service says "I'm alive" on a schedule (works behind NAT);
* ``probe``     — this app polls a service's health URL (needs reachability);
* ``manual``    — set by hand during an incident.

**A status goes stale rather than staying true.** If a heartbeat or probe has
not reported inside `STATUS_STALE_AFTER_SECONDS`, the service reads ``unknown``
— never ``operational``. A status page that claims everything is fine because
nothing has reported in is worse than one that admits it does not know, and
that is the failure mode these pages are famous for.

Manual statuses do not go stale: a human saying "this is down" stands until a
human says otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .database import service_status_collection, service_status_days_collection

OPERATIONAL = "operational"
DEGRADED = "degraded"
DOWN = "down"
MAINTENANCE = "maintenance"
UNKNOWN = "unknown"

# Worst-first, so `worst()` can pick the headline status for the whole page.
SEVERITY = {DOWN: 4, DEGRADED: 3, MAINTENANCE: 2, UNKNOWN: 1, OPERATIONAL: 0}
STATUSES = tuple(SEVERITY)

SOURCE_HEARTBEAT = "heartbeat"
SOURCE_PROBE = "probe"
SOURCE_MANUAL = "manual"


@dataclass(frozen=True)
class SystemDef:
    key: str
    name: str
    components: int


# Mirrors the six systems the status page lists, including their component
# counts. Deliberately its own list rather than derived from the rate card:
# OCR is shown here but is not a billed service, and "API Dashboard" is this
# platform itself.
SYSTEMS: dict[str, SystemDef] = {
    s.key: s
    for s in [
        SystemDef("tts", "Text to Speech", 2),
        SystemDef("stt", "Speech to Text", 6),
        SystemDef("translate", "Translate", 3),
        SystemDef("chat", "Chat API", 1),
        SystemDef("ocr", "Document Digitization (OCR)", 1),
        SystemDef("dashboard", "API Dashboard", 1),
    ]
}


class UnknownSystemError(ValueError):
    pass


def get_system(key: str) -> SystemDef:
    system = SYSTEMS.get((key or "").lower().strip())
    if system is None:
        raise UnknownSystemError(
            f"Unknown system '{key}'. Valid: {', '.join(SYSTEMS)}"
        )
    return system


def worst(statuses) -> str:
    """The most severe status in a group — the headline for the whole page."""
    ranked = [s for s in statuses if s in SEVERITY]
    if not ranked:
        return UNKNOWN
    return max(ranked, key=lambda s: SEVERITY[s])


def _day_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def apply_staleness(doc: dict | None, now: datetime | None = None) -> str:
    """The status as it should be *read*, accounting for silence.

    A heartbeat or probe that stopped reporting means we no longer know, so it
    reads `unknown`. A manual status is a human assertion and stands.
    """
    if doc is None:
        return UNKNOWN
    status = doc.get("status", UNKNOWN)
    if doc.get("source") == SOURCE_MANUAL:
        return status

    reported = doc.get("reported_at")
    if reported is None:
        return UNKNOWN
    if reported.tzinfo is None:
        reported = reported.replace(tzinfo=timezone.utc)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        seconds=get_settings().status_stale_after_seconds
    )
    return status if reported >= cutoff else UNKNOWN


async def record(key: str, status: str, source: str, detail: str | None = None) -> dict:
    """Store a status report and fold it into the day's rollup."""
    system = get_system(key)
    if status not in SEVERITY:
        raise ValueError(f"Unknown status '{status}'. Valid: {', '.join(STATUSES)}")

    now = datetime.now(timezone.utc)
    document = {
        "service": system.key,
        "status": status,
        "source": source,
        "detail": detail,
        "reported_at": now,
    }
    await service_status_collection().update_one(
        {"service": system.key}, {"$set": document}, upsert=True
    )

    # The 90-bar strip shows the *worst* status each day, not the latest: a day
    # containing an outage should stay red once it recovers, or the history
    # quietly erases every incident that was fixed.
    day = _day_key(now)
    existing = await service_status_days_collection().find_one(
        {"service": system.key, "day": day}
    )
    rolled = worst([status, existing.get("status", OPERATIONAL)]) if existing else status
    await service_status_days_collection().update_one(
        {"service": system.key, "day": day},
        {
            "$set": {"service": system.key, "day": day, "status": rolled},
            "$inc": {"reports": 1},
        },
        upsert=True,
    )
    return document


async def history(key: str, days: int, now: datetime | None = None) -> list[dict]:
    """Daily status for the last `days`, oldest first, gaps filled `unknown`."""
    today = (now or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = today - timedelta(days=days - 1)
    rows = await service_status_days_collection().find(
        {"service": key, "day": {"$gte": _day_key(start)}}
    ).to_list(length=None)
    found = {row["day"]: row.get("status", UNKNOWN) for row in rows}

    # Days before the service ever reported are `unknown`, not `operational` —
    # we have no evidence about them either way.
    return [
        {
            "date": _day_key(start + timedelta(days=offset)),
            "status": found.get(_day_key(start + timedelta(days=offset)), UNKNOWN),
        }
        for offset in range(days)
    ]
