"""Engine burn — how much of *our* own capacity customers are using.

    GET /engines/usage        per-engine consumption vs the configured quota

This is the other half of metering. `GET /usage/summary` answers "what does
this customer owe us"; this answers "what has serving them cost us in engine
capacity, and how much of the allowance is left".

**Gated by an operator secret, not a user session.** Every other authenticated
endpoint here answers *about the caller*; this one answers about the business.
Consumption and remaining allowance are facts about our margin, so exposing
them to a signed-in customer — who is often the very account being reported on
— would hand competitors the cost side of our pricing. With `OPS_SECRET` unset
the endpoint returns 503 rather than falling open, on the same principle as the
billing and status secrets.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi import status as http

from .. import engines
from ..config import get_settings
from ..database import usage_collection, users_collection
from ..serialization import iso

router = APIRouter(prefix="/engines", tags=["engines"])

# How many accounts to name per engine. The point is to spot the handful of
# callers burning the allowance, not to page through every customer.
_TOP_ACCOUNTS = 5


def _require_ops_secret(provided: str | None) -> None:
    settings = get_settings()
    if not settings.ops_secret:
        raise HTTPException(
            status_code=http.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Engine reporting is not configured (OPS_SECRET unset).",
        )
    # Constant-time: a plain `!=` leaks the secret a character at a time.
    if not provided or not secrets.compare_digest(provided, settings.ops_secret):
        raise HTTPException(
            status_code=http.HTTP_401_UNAUTHORIZED, detail="Invalid ops secret."
        )


@router.get("/usage")
async def engine_usage(
    x_ops_secret: str | None = Header(default=None, alias="X-Ops-Secret"),
    days: int = Query(default=30, ge=1, le=365),
):
    """Engine consumption over the last `days`, against the configured quota.

    The window is a **rolling period, not a billing cycle**, which this service
    has no way to know. So `consumed` answers "how much did we burn in the last
    N days", and `remaining` is only meaningful when the period covers the whole
    life of the allowance — which is why the response states its own period
    rather than implying a balance.
    """
    _require_ops_secret(x_ops_secret)

    finish = datetime.now(timezone.utc)
    begin = finish - timedelta(days=days)

    # Grouped by (engine, account) in one pass: the per-engine totals and the
    # top consumers are the same aggregation viewed two ways, and doing it
    # twice would let the two disagree if a write landed in between.
    pipeline = [
        {
            "$match": {
                "engine": {"$ne": None},
                "created_at": {"$gte": begin, "$lte": finish},
            }
        },
        {
            "$group": {
                "_id": {"engine": "$engine", "user_id": "$user_id"},
                "consumed": {"$sum": "$engine_quantity"},
                "events": {"$sum": 1},
            }
        },
    ]
    rows = await usage_collection().aggregate(pipeline).to_list(length=None)

    by_engine: dict[str, dict] = {}
    for row in rows:
        key = row["_id"]["engine"]
        bucket = by_engine.setdefault(key, {"consumed": 0.0, "events": 0, "accounts": []})
        consumed = float(row.get("consumed") or 0)
        bucket["consumed"] += consumed
        bucket["events"] += row["events"]
        bucket["accounts"].append(
            {"user_id": row["_id"]["user_id"], "consumed": consumed, "events": row["events"]}
        )

    quotas = get_settings().engine_quota_map
    labels = await _account_labels(by_engine)

    data = []
    # Every known engine is listed, including ones with no traffic: an engine
    # missing from the page is indistinguishable from one nobody has reported
    # for, and silence is exactly what a capacity view must not hide.
    for key in sorted(set(engines.ENGINES) | set(by_engine)):
        bucket = by_engine.get(key, {"consumed": 0.0, "events": 0, "accounts": []})
        line = engines.summarise(key, bucket["consumed"], bucket["events"], quotas)
        top = sorted(bucket["accounts"], key=lambda a: a["consumed"], reverse=True)
        line["top_accounts"] = [
            {
                "user_id": str(a["user_id"]),
                "email": labels.get(str(a["user_id"]), "(deleted account)"),
                "consumed": round(a["consumed"], 4),
                "events": a["events"],
            }
            for a in top[:_TOP_ACCOUNTS]
        ]
        data.append(line)

    return {
        "status": True,
        "period": {"days": days, "from": iso(begin), "to": iso(finish)},
        # Says plainly whether `remaining` means anything yet, so an operator
        # reading nulls knows it is unconfigured rather than broken.
        "quotas_configured": bool(quotas),
        "data": data,
    }


async def _account_labels(by_engine: dict[str, dict]) -> dict[str, str]:
    """Email per account id, for the accounts about to be listed.

    Only the ids that will actually be shown are looked up — the aggregation
    can span every customer, and fetching them all to render five rows per
    engine would make this endpoint scale with the customer base.
    """
    wanted: set[str] = set()
    for bucket in by_engine.values():
        top = sorted(bucket["accounts"], key=lambda a: a["consumed"], reverse=True)
        wanted.update(str(a["user_id"]) for a in top[:_TOP_ACCOUNTS])

    oids = []
    for value in wanted:
        try:
            oids.append(ObjectId(value))
        except Exception:
            continue
    if not oids:
        return {}

    cursor = users_collection().find({"_id": {"$in": oids}}, {"email": 1})
    return {str(doc["_id"]): doc.get("email", "") async for doc in cursor}
