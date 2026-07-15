"""Usage metering and billing.

* ``POST /usage``          — record consumption (auth: X-API-Key). Computes and
                             stores the INR cost for the reported quantity.
* ``POST /usage/estimate`` — compute a cost without recording (no auth).
* ``GET  /usage``          — the caller's usage history (auth: Bearer JWT).
* ``GET  /usage/summary``  — per-service totals + grand total (auth: Bearer JWT).

Billing is purely usage-based: ``cost = rate * quantity / unit_size`` (see
``pricing.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pymongo.errors import DuplicateKeyError

from .. import money
from ..config import get_settings
from ..database import indexes_ready, usage_collection
from ..deps import get_api_user, get_current_user
from ..models.usage import UsageRequest
from ..pricing import UnknownServiceError, calculate_cost, get_service
from ..serialization import serialize_docs

router = APIRouter(prefix="/usage", tags=["usage"])

# Fields of a priced record that carry money and must not become floats until
# they reach JSON. Everything else in `_price` is a label, a count or a unit.
_MONEY_FIELDS = frozenset({"rate", "cost"})


def _price(service_key: str, quantity: float) -> dict:
    try:
        service = get_service(service_key)
        cost = calculate_cost(service_key, quantity)
    except UnknownServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {
        "service": service.key,
        "label": service.label,
        "unit": service.unit,
        "quantity": quantity,
        "rate": service.rate,  # Decimal
        "unit_size": service.unit_size,
        "cost": cost,  # Decimal
        "currency": get_settings().currency,
    }


def _for_json(priced: dict) -> dict:
    """Amounts -> float, at the response boundary."""
    return {
        k: money.to_json(v) if k in _MONEY_FIELDS else v for k, v in priced.items()
    }


def _for_bson(priced: dict) -> dict:
    """Amounts -> Decimal128, so Mongo stores and `$sum`s them exactly."""
    return {
        k: money.to_bson(v) if k in _MONEY_FIELDS else v for k, v in priced.items()
    }


@router.post("/estimate")
async def estimate(payload: UsageRequest):
    """Return the cost of a hypothetical usage. No auth, nothing stored."""
    return {"status": True, "data": _for_json(_price(payload.service, payload.quantity))}


# The priced fields stored on every usage record, echoed back to the caller.
_PRICED_FIELDS = (
    "service",
    "label",
    "unit",
    "quantity",
    "rate",
    "unit_size",
    "cost",
    "currency",
)


def _utc_now_ms() -> datetime:
    """UTC now, truncated to milliseconds.

    BSON datetimes only hold milliseconds, so Mongo drops anything finer on
    write. Truncating up front keeps the timestamp in the 201 response equal to
    the one `GET /usage` and any replay read back, instead of advertising
    microsecond precision the database never stored.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _recorded(doc: dict) -> dict:
    """Render a stored usage record.

    Built from the document rather than the freshly-priced values so a replay
    returns exactly what the first call stored, even if the rate card has
    changed since.
    """
    created_at = doc["created_at"]
    data = {}
    for field in _PRICED_FIELDS:
        value = doc.get(field)
        if field in _MONEY_FIELDS and value is not None:
            value = money.to_json(value)
        data[field] = value
    return {
        "id": str(doc["_id"]),
        **data,
        "created_at": getattr(created_at, "isoformat", lambda: created_at)(),
    }


async def _find_replay(user_id, idempotency_key: str) -> dict | None:
    return await usage_collection().find_one(
        {"user_id": user_id, "idempotency_key": idempotency_key}
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def record_usage(
    payload: UsageRequest,
    response: Response,
    user: dict = Depends(get_api_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Unique value per usage event. Retrying with the same key returns "
            "the original record (200) instead of billing the customer twice."
        ),
    ),
):
    """Record real consumption against the API key's owner.

    This is the billing write, so a network timeout must be safe to retry.
    Send an `Idempotency-Key` and a replay returns the record the first call
    stored, with 200 rather than 201, charging the customer once.
    """
    priced = _price(payload.service, payload.quantity)
    document = {
        "user_id": user["_id"],
        "api_key_id": user.get("_api_key_id"),
        **_for_bson(priced),
        "metadata": payload.metadata,
        "created_at": _utc_now_ms(),
    }

    if idempotency_key:
        document["idempotency_key"] = idempotency_key
        # The unique index normally catches replays via DuplicateKeyError on
        # insert, which keeps the common (non-replay) path at one round trip.
        # While that index is missing there is nothing to raise, so check first.
        if not indexes_ready():
            existing = await _find_replay(user["_id"], idempotency_key)
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return {"status": True, "replayed": True, "data": _recorded(existing)}

    try:
        result = await usage_collection().insert_one(document)
    except DuplicateKeyError:
        existing = await _find_replay(user["_id"], idempotency_key)
        if existing is None:  # lost a race with a concurrent identical request
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key is already in flight. Retry shortly.",
            )
        response.status_code = status.HTTP_200_OK
        return {"status": True, "replayed": True, "data": _recorded(existing)}

    document["_id"] = result.inserted_id
    return {"status": True, "replayed": False, "data": _recorded(document)}


@router.get("")
async def usage_history(
    user: dict = Depends(get_current_user),
    service: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    query: dict = {"user_id": user["_id"]}
    if service:
        query["service"] = service
    cursor = usage_collection().find(query).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return {"status": True, "count": len(docs), "data": serialize_docs(docs)}


@router.get("/summary")
async def usage_summary(user: dict = Depends(get_current_user)):
    """Aggregate the caller's spend by service, plus a grand total."""
    pipeline = [
        {"$match": {"user_id": user["_id"]}},
        {
            "$group": {
                "_id": "$service",
                "label": {"$first": "$label"},
                "unit": {"$first": "$unit"},
                "total_quantity": {"$sum": "$quantity"},
                "total_cost": {"$sum": "$cost"},
                "events": {"$sum": 1},
            }
        },
    ]
    rows = await usage_collection().aggregate(pipeline).to_list(length=None)

    # `$sum` over Decimal128 is exact, so these totals are the invoice figures.
    # Ordered here rather than with a `$sort` stage: there is only one row per
    # service (eight at most) and they are all in memory already, so this costs
    # nothing and keeps Decimal128 ordering out of the aggregation layer.
    ranked = sorted(
        ((row, money.to_decimal(row.get("total_cost", 0))) for row in rows),
        key=lambda pair: pair[1],
        reverse=True,
    )

    by_service = [
        {
            "service": row["_id"],
            "label": row.get("label"),
            "unit": row.get("unit"),
            "total_quantity": round(row.get("total_quantity", 0), 4),
            "total_cost": money.to_json(cost),
            "events": row.get("events", 0),
        }
        for row, cost in ranked
    ]
    # Summed from the Decimals, not the rendered floats above — adding those
    # back up is exactly what would reintroduce the drift.
    return {
        "status": True,
        "currency": get_settings().currency,
        "grand_total": money.to_json(money.total(cost for _, cost in ranked)),
        "by_service": by_service,
    }
