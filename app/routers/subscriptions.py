"""App-launch notification subscriptions.

    POST /subscriptions/v1/notify-app-launch   { "email": "..." }

The frontend (NotifyMe / downloads) expects:
* success  -> HTTP 2xx (any ok body);
* duplicate -> non-ok HTTP with ``{ "err_code": 409, "error": "<message>" }``.

This endpoint's error contract differs from the blog envelope on purpose — it
matches exactly what the existing client code branches on.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from ..database import subscriptions_collection
from ..models.requests import SubscriptionRequest

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("/v1/notify-app-launch")
async def notify_app_launch(payload: SubscriptionRequest):
    email = payload.email.lower().strip()
    collection = subscriptions_collection()

    # Guard against duplicates even without a unique index present.
    existing = await collection.find_one({"email": email})
    if existing:
        return JSONResponse(
            status_code=409,
            content={"err_code": 409, "error": "You have already subscribed."},
        )

    document = {
        "email": email,
        "source": "notify-app-launch",
        "subscribedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await collection.insert_one(document)
    except DuplicateKeyError:
        return JSONResponse(
            status_code=409,
            content={"err_code": 409, "error": "You have already subscribed."},
        )

    return JSONResponse(
        status_code=201,
        content={
            "status": True,
            "status_code": 201,
            "message": "Subscribed successfully.",
        },
    )
