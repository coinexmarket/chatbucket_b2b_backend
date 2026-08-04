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

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pymongo.errors import DuplicateKeyError

from .. import analytics, credits, money, ratelimit
from ..analytics import RangeTooLargeError
from ..config import get_settings
from ..database import (
    api_keys_collection,
    indexes_ready,
    projects_collection,
    usage_collection,
)
from ..deps import get_current_user, get_metering_user
from ..models.usage import UsageRequest
from ..plans import get_plan
from ..pricing import (
    UnknownServiceError,
    calculate_cost,
    calculate_split_cost,
    get_service,
    normalize_model_key,
    resolve_rate,
    split_rates,
)
from ..serialization import iso, serialize_docs

router = APIRouter(prefix="/usage", tags=["usage"])

# Fields of a priced record that carry money and must not become floats until
# they reach JSON. Everything else in `_price` is a label, a count or a unit.
_MONEY_FIELDS = frozenset({"rate", "cost", "input_rate", "output_rate"})


def _price(payload: UsageRequest) -> dict:
    """Price one reported consumption, flat or input/output split.

    The stored record carries whichever rates were actually applied, so history
    stays auditable after a price change or a switch to split pricing.
    """
    try:
        service = get_service(payload.service)
        currency = get_settings().currency
        base = {
            "service": service.key,
            "label": service.label,
            "unit": service.unit,
            "quantity": payload.total_quantity,
            "currency": currency,
        }

        if payload.input_quantity is not None:
            rates = split_rates(payload.service, payload.model)
            if rates is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Service '{service.key}' is not priced separately for "
                        "input and output. Send `quantity` instead."
                    ),
                )
            input_rate, output_rate, unit_size = rates
            return {
                **base,
                "pricing": "split",
                "input_quantity": payload.input_quantity,
                "output_quantity": payload.output_quantity,
                "input_rate": input_rate,  # Decimal
                "output_rate": output_rate,  # Decimal
                "rate": None,  # no single rate applies
                "unit_size": unit_size,
                "cost": calculate_split_cost(
                    payload.service,
                    payload.input_quantity,
                    payload.output_quantity,
                    payload.model,
                ),
            }

        # The model can carry its own price; `rate`/`unit_size` below are the
        # ones actually charged, so the stored record shows what was billed
        # rather than the service default.
        rate, unit_size, _ = resolve_rate(payload.service, payload.model)
        return {
            **base,
            "pricing": "flat",
            "input_quantity": None,
            "output_quantity": None,
            "input_rate": None,
            "output_rate": None,
            "rate": rate,  # Decimal
            "unit_size": unit_size,
            "cost": calculate_cost(payload.service, payload.quantity, payload.model),
        }
    except UnknownServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _for_json(priced: dict) -> dict:
    """Amounts -> float, at the response boundary. None stays None."""
    return {
        k: money.to_json(v) if (k in _MONEY_FIELDS and v is not None) else v
        for k, v in priced.items()
    }


def _for_bson(priced: dict) -> dict:
    """Amounts -> Decimal128, so Mongo stores and `$sum`s them exactly."""
    return {
        k: money.to_bson(v) if (k in _MONEY_FIELDS and v is not None) else v
        for k, v in priced.items()
    }


@router.post("/estimate")
async def estimate(payload: UsageRequest):
    """Return the cost of a hypothetical usage. No auth, nothing stored.

    Takes `model` too, so an estimate matches what `POST /usage` will actually
    charge when that model has its own price.
    """
    return {
        "status": True,
        "data": _for_json(_price(payload)),
    }


# The priced fields stored on every usage record, echoed back to the caller.
_PRICED_FIELDS = (
    "service",
    "label",
    "unit",
    "quantity",
    "pricing",
    "input_quantity",
    "output_quantity",
    "input_rate",
    "output_rate",
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
        "model": doc.get("model"),
        # False means the consumption was recorded but the customer had no
        # credits for it — the caller needs to see that, not just the 402.
        "billed": bool(doc.get("billed", False)),
        "created_at": iso(created_at),
    }


async def _enforce_plan_limit(user: dict, service: str, response: Response) -> None:
    """Apply the caller's plan rate limit and set the `X-RateLimit-*` headers.

    Scoped to (account, service) rather than to the API key: every key on an
    account shares one allowance, so minting extra keys cannot buy extra
    throughput.
    """
    if not get_settings().enforce_plan_rate_limits:
        return
    plan = get_plan(user.get("plan"))
    limit = ratelimit.Limit(plan.requests_per_minute, 60)
    result = await ratelimit.enforce_limit(
        "plan", f"{user['_id']}:{service}", limit
    )
    response.headers["X-RateLimit-Limit"] = str(limit.requests)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(result.retry_after)


async def _find_replay(user_id, idempotency_key: str) -> dict | None:
    return await usage_collection().find_one(
        {"user_id": user_id, "idempotency_key": idempotency_key}
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def record_usage(
    payload: UsageRequest,
    response: Response,
    user: dict = Depends(get_metering_user),
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
    # The plan's advertised requests-per-minute, applied per service and shared
    # across the account's API keys — exactly what `GET /limits` reports.
    # Checked before pricing so a throttled call costs nothing.
    await _enforce_plan_limit(user, payload.service, response)

    priced = _price(payload)
    document = {
        "user_id": user["_id"],
        "api_key_id": user.get("_api_key_id"),
        "project_id": user.get("_api_key_project_id"),
        **_for_bson(priced),
        # `model` is kept as the caller spelled it, for display; `model_key` is
        # the normalised value everything groups on, so "CB Paluku" and
        # "cb  paluku" are one row rather than two.
        "model": payload.model,
        "model_key": normalize_model_key(payload.model) if payload.model else None,
        # What the call cost *us* in engine capacity. Stored beside the
        # billable figures but never priced with them: this is COGS, and the
        # customer neither pays it nor sees it.
        "engine": payload.engine,
        "engine_quantity": payload.engine_quantity,
        "metadata": payload.metadata,
        # Flipped to True once the credits are actually taken, below. Recorded
        # up front so usage is never lost just because it could not be paid for.
        "billed": False,
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

    # Take the credits only after the insert, so the unique idempotency index
    # has already rejected any replay: a retried call must never be charged
    # twice. The debit itself is a single conditional update, so two concurrent
    # calls cannot both spend a balance that only covers one.
    charged = await _charge(document, priced["cost"])
    body = {"status": True, "replayed": False, "data": _recorded(document)}
    if charged is not None:
        body["balance"] = charged
        return body

    # Recorded but unpaid. 402 tells the calling service to stop serving this
    # customer; the record stays as evidence of what was consumed.
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=(
            "Insufficient credits. The usage was recorded but not billed. "
            "Top up at /billing/top-up."
        ),
    )


def _charge_description(document: dict) -> str:
    return f"{document.get('label') or document.get('service')} usage"


async def _charge(document: dict, cost) -> float | None:
    """Debit the cost from the owner's credits. Returns the new balance.

    Returns None when the balance will not cover it. With enforcement disabled
    the service meters without ever blocking, which is how it behaved before
    credits existed.
    """
    units = credits.to_units(cost)

    if not get_settings().enforce_credit_balance:
        # Still debit, so balances and the ledger stay truthful; just never
        # refuse. The balance is allowed to go negative, which is the honest
        # record of what an un-enforced deployment let through.
        entry = await credits.debit_allowing_negative(
            document["user_id"], units, _charge_description(document), ref=document["_id"]
        )
        await usage_collection().update_one(
            {"_id": document["_id"]},
            {"$set": {"billed": True, "ledger_id": entry["_id"]}},
        )
        document["billed"] = True
        return money.to_json(credits.from_units(int(entry["balance_after_units"])))

    entry = await credits.try_debit(
        document["user_id"], units, _charge_description(document), ref=document["_id"]
    )
    if entry is None:
        return None

    await usage_collection().update_one(
        {"_id": document["_id"]},
        {"$set": {"billed": True, "ledger_id": entry["_id"]}},
    )
    document["billed"] = True
    return money.to_json(credits.from_units(int(entry["balance_after_units"])))


def _scope(
    user: dict,
    service: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """The Mongo filter for one customer's usage, with optional narrowing.

    Always anchored on `user_id`, so no combination of query parameters can
    reach another customer's records.
    """
    query: dict = {"user_id": user["_id"]}
    if project_id:
        query["project_id"] = project_id
    if service:
        query["service"] = service
    if model:
        # Matched on the normalised key, so the filter is case- and
        # spacing-insensitive in the same way the grouping is.
        query["model_key"] = normalize_model_key(model)
    if api_key_id:
        # Stored as a string on the usage record (see `deps.get_api_user`); an
        # unparseable id simply matches nothing rather than erroring.
        query["api_key_id"] = api_key_id
    return query


@router.get("")
async def usage_history(
    user: dict = Depends(get_current_user),
    service: str | None = Query(default=None),
    model: str | None = Query(default=None),
    api_key_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    query = _scope(user, service, model, api_key_id, project_id)
    cursor = (
        usage_collection()
        # Which engine served the call, and what it consumed, is our
        # commercial information — it is our margin. Projected away here
        # rather than filtered in Python so it never reaches the process
        # boundary at all.
        .find(query, {"engine": 0, "engine_quantity": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    return {"status": True, "count": len(docs), "data": serialize_docs(docs)}


async def _period_totals(user: dict, begin, finish) -> dict:
    rows = await usage_collection().aggregate([
        {"$match": {"user_id": user["_id"], "created_at": {"$gte": begin, "$lt": finish}}},
        {
            "$group": {
                "_id": None,
                "cost": {"$sum": "$cost"},
                "quantity": {"$sum": "$quantity"},
                "requests": {"$sum": 1},
            }
        },
    ]).to_list(length=None)
    row = rows[0] if rows else {}
    return {
        "cost": money.to_decimal(row.get("cost", 0)),
        "quantity": round(row.get("quantity", 0), 4),
        "requests": row.get("requests", 0),
    }


def _percent_change(current: Decimal, previous: Decimal) -> float | None:
    """Percent change, or None when there is no baseline to compare against.

    Returning 0 or 100 for "no usage last period" would both be lies; the
    dashboard should render a dash instead of inventing a trend.
    """
    if previous == 0:
        return None
    return float(round((current - previous) / previous * 100, 2))


@router.get("/overview")
async def usage_overview(
    user: dict = Depends(get_current_user),
    days: int = Query(default=30, ge=1, le=365),
):
    """Headline figures for the dashboard, against the preceding period.

    The comparison window is the same length immediately before, so "30 days"
    is measured against the 30 days before that rather than a calendar month.
    """
    now = datetime.now(timezone.utc)
    span = timedelta(days=days)
    current_start, previous_start = now - span, now - (span * 2)

    current = await _period_totals(user, current_start, now)
    previous = await _period_totals(user, previous_start, current_start)
    balance = credits.from_units(await credits.balance_units(user["_id"]))

    return {
        "status": True,
        "currency": get_settings().currency,
        "plan": get_plan(user.get("plan")).key,
        "credits": money.to_json(balance),
        "period": {
            "days": days,
            "from": current_start.isoformat(),
            "to": now.isoformat(),
        },
        "current": {
            "cost": money.to_json(current["cost"]),
            "requests": current["requests"],
            "quantity": current["quantity"],
        },
        "previous": {
            "cost": money.to_json(previous["cost"]),
            "requests": previous["requests"],
            "quantity": previous["quantity"],
        },
        "change_percent": {
            "cost": _percent_change(current["cost"], previous["cost"]),
            "requests": _percent_change(
                Decimal(current["requests"]), Decimal(previous["requests"])
            ),
        },
    }


@router.get("/timeseries")
async def usage_timeseries(
    user: dict = Depends(get_current_user),
    granularity: str = Query(default="daily", description="daily | hourly | minute"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    service: str | None = Query(default=None),
    model: str | None = Query(default=None),
    api_key_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
):
    """Spend, requests and quantity bucketed over time, for the usage charts.

    Buckets with no usage are returned as zeroes rather than omitted: dropping
    them would make a quiet day vanish from the x-axis instead of showing as a
    flat line.
    """
    try:
        gran = analytics.get_granularity(granularity)
        begin, finish = analytics.resolve_range(gran, from_, to)
    except RangeTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    query = _scope(user, service, model, api_key_id, project_id)
    query["created_at"] = {"$gte": analytics.truncate(begin, gran), "$lte": finish}

    rows = await usage_collection().aggregate([
        {"$match": query},
        {
            "$group": {
                "_id": {"$dateToString": {"format": gran.fmt, "date": "$created_at"}},
                "cost": {"$sum": "$cost"},
                "quantity": {"$sum": "$quantity"},
                "requests": {"$sum": 1},
            }
        },
    ]).to_list(length=None)
    found = {row["_id"]: row for row in rows}

    buckets = []
    for label in analytics.bucket_labels(begin, finish, gran):
        row = found.get(label)
        buckets.append({
            "bucket": label,
            "cost": money.to_json(row["cost"]) if row else 0.0,
            "quantity": round(row["quantity"], 4) if row else 0.0,
            "requests": row["requests"] if row else 0,
        })

    return {
        "status": True,
        "granularity": gran.key,
        "from": begin.isoformat(),
        "to": finish.isoformat(),
        "currency": get_settings().currency,
        "totals": {
            "cost": money.to_json(money.total(b["cost"] for b in buckets)),
            "requests": sum(b["requests"] for b in buckets),
            "quantity": round(sum(b["quantity"] for b in buckets), 4),
        },
        "count": len(buckets),
        "data": buckets,
    }


_CSV_COLUMNS = (
    "created_at", "service", "label", "model", "quantity",
    "input_quantity", "output_quantity", "unit",
    "rate", "input_rate", "output_rate", "unit_size",
    "cost", "currency", "billed", "api_key_id", "project_id",
)


@router.get("/export.csv")
async def export_usage_csv(
    user: dict = Depends(get_current_user),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    service: str | None = Query(default=None),
    model: str | None = Query(default=None),
    api_key_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=50_000, ge=1, le=200_000),
):
    """Usage records as CSV, for spreadsheets and reconciliation.

    Streamed rather than assembled in memory: a year of metering is far more
    rows than should ever be held as one string before the first byte is sent.
    """
    query = _scope(user, service, model, api_key_id, project_id)
    try:
        if from_ or to:
            window: dict = {}
            if from_:
                window["$gte"] = analytics.parse_instant(from_)
            if to:
                window["$lte"] = analytics.parse_instant(to)
            query["created_at"] = window
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    cursor = usage_collection().find(query).sort("created_at", -1).limit(limit)

    async def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")

        def flush() -> str:
            value = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return value

        writer.writerow(_CSV_COLUMNS)
        yield flush()

        async for doc in cursor:
            created = doc.get("created_at")
            writer.writerow([
                iso(created),
                doc.get("service"),
                doc.get("label"),
                doc.get("model") or "",
                doc.get("quantity"),
                doc.get("input_quantity") if doc.get("input_quantity") is not None else "",
                doc.get("output_quantity") if doc.get("output_quantity") is not None else "",
                doc.get("unit"),
                # Rendered through `money` so the CSV carries the exact stored
                # decimal, not a float that has been through a JSON round trip.
                money.to_json(doc["rate"]) if doc.get("rate") is not None else "",
                money.to_json(doc["input_rate"]) if doc.get("input_rate") is not None else "",
                money.to_json(doc["output_rate"]) if doc.get("output_rate") is not None else "",
                doc.get("unit_size"),
                money.to_json(doc["cost"]) if doc.get("cost") is not None else "",
                doc.get("currency"),
                "yes" if doc.get("billed") else "no",
                doc.get("api_key_id") or "",
                doc.get("project_id") or "",
            ])
            yield flush()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="usage-{stamp}.csv"'},
    )


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

    # Consumption that ran out of credits is recorded but never charged.
    # Folding it into one total would overstate what the customer actually
    # paid, so it is reported as its own figure.
    unbilled_docs = await usage_collection().find(
        {"user_id": user["_id"], "billed": False}, {"cost": 1}
    ).to_list(length=None)
    unbilled = money.total(d.get("cost", 0) for d in unbilled_docs)

    # Per-model breakdown. Only records that reported a model take part; the
    # rest are reported as `unattributed_cost` so the rows still reconcile
    # against `grand_total` instead of quietly not adding up.
    model_rows = await usage_collection().aggregate([
        {"$match": {"user_id": user["_id"], "model_key": {"$ne": None}}},
        {
            "$group": {
                "_id": "$model_key",
                "model": {"$first": "$model"},
                "total_quantity": {"$sum": "$quantity"},
                "total_cost": {"$sum": "$cost"},
                "events": {"$sum": 1},
            }
        },
    ]).to_list(length=None)

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
    # Per-API-key breakdown, labelled with each key's name so the dashboard's
    # key filter can show "Production" rather than a raw ObjectId.
    key_rows = await usage_collection().aggregate([
        {"$match": {"user_id": user["_id"], "api_key_id": {"$ne": None}}},
        {
            "$group": {
                "_id": "$api_key_id",
                "total_cost": {"$sum": "$cost"},
                "events": {"$sum": 1},
            }
        },
    ]).to_list(length=None)

    key_docs = await api_keys_collection().find({"user_id": user["_id"]}).to_list(length=None)
    key_names = {
        str(d["_id"]): {
            "name": d.get("name"),
            "masked_key": f"{d.get('key_prefix', 'cb_live')}_****{d.get('key_last4', '')}",
            "revoked": d.get("revoked", False),
        }
        for d in key_docs
    }

    ranked_keys = sorted(
        ((row, money.to_decimal(row.get("total_cost", 0))) for row in key_rows),
        key=lambda pair: pair[1],
        reverse=True,
    )
    by_api_key = [
        {
            "api_key_id": row["_id"],
            # A key deleted from the collection would leave usage with no
            # label; name it rather than rendering a blank row.
            **key_names.get(row["_id"], {"name": "(deleted key)", "masked_key": None, "revoked": True}),
            "total_cost": money.to_json(cost),
            "events": row.get("events", 0),
        }
        for row, cost in ranked_keys
    ]

    # Per-project breakdown, labelled from the project list.
    project_rows = await usage_collection().aggregate([
        {"$match": {"user_id": user["_id"], "project_id": {"$ne": None}}},
        {"$group": {"_id": "$project_id", "total_cost": {"$sum": "$cost"}, "events": {"$sum": 1}}},
    ]).to_list(length=None)
    project_docs = await projects_collection().find({"user_id": user["_id"]}).to_list(length=None)
    project_names = {str(d["_id"]): d.get("name") for d in project_docs}
    ranked_projects = sorted(
        ((row, money.to_decimal(row.get("total_cost", 0))) for row in project_rows),
        key=lambda pair: pair[1],
        reverse=True,
    )
    by_project = [
        {
            "project_id": row["_id"],
            # Usage outlives the project it was recorded under, by design —
            # deleting a project must not rewrite what a period cost.
            "name": project_names.get(row["_id"], "(deleted project)"),
            "total_cost": money.to_json(cost),
            "events": row.get("events", 0),
        }
        for row, cost in ranked_projects
    ]

    ranked_models = sorted(
        ((row, money.to_decimal(row.get("total_cost", 0))) for row in model_rows),
        key=lambda pair: pair[1],
        reverse=True,
    )
    attributed = money.total(cost for _, cost in ranked_models)
    grand_total = money.total(cost for _, cost in ranked)

    by_model = [
        {
            "model": row.get("model"),
            "model_key": row["_id"],
            "total_quantity": round(row.get("total_quantity", 0), 4),
            "total_cost": money.to_json(cost),
            "events": row.get("events", 0),
            # What this model accounts for, which is what the usage table's
            # percentage column shows.
            "share_percent": (
                float(round(cost / grand_total * 100, 2)) if grand_total else 0.0
            ),
        }
        for row, cost in ranked_models
    ]

    return {
        "status": True,
        "currency": get_settings().currency,
        # Everything consumed...
        "grand_total": money.to_json(grand_total),
        # ...of which this much was never paid for (credits exhausted)...
        "unbilled_total": money.to_json(unbilled),
        # ...leaving what the customer was actually charged.
        "billed_total": money.to_json(grand_total - unbilled),
        "by_service": by_service,
        "by_model": by_model,
        "by_api_key": by_api_key,
        "by_project": by_project,
        # Consumption from callers that did not report a model. Non-zero here
        # means a service is metering without sending `model`.
        "unattributed_cost": money.to_json(grand_total - attributed),
    }
