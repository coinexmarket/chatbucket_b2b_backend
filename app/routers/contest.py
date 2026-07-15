"""Hackathon contest registration + verification.

Python port of the existing Next.js ``/api/register`` and ``/api/verify``
routes. These write to a *separate* database (``ChatBucketHackathon`` by
default) and the ``contest_registrations`` collection, matching the data the
current site already stores.

Response shapes are kept byte-for-byte compatible with the frontend:
* register -> ``{ success, data: { referenceNumber, insertedId } }``
* verify   -> ``{ valid, name?, isWinner? }``
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..database import contest_registrations_collection
from ..models.requests import ContestRegistrationRequest

router = APIRouter(prefix="/api", tags=["contest"])

_REF_ALPHABET = string.ascii_uppercase + string.digits


def _reference_number() -> str:
    # Mirrors the JS: 'WX-' + 6 uppercase base36-ish chars.
    suffix = "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))
    return f"WX-{suffix}"


@router.post("/register")
async def register(payload: ContestRegistrationRequest):
    reference_number = _reference_number()
    document = {
        **payload.model_dump(),
        "referenceNumber": reference_number,
        "registeredAt": datetime.now(timezone.utc).isoformat(),
    }

    result = await contest_registrations_collection().insert_one(document)

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "data": {
                "insertedId": str(result.inserted_id),
                "referenceNumber": reference_number,
            },
        },
    )


@router.get("/verify")
async def verify(id: str | None = Query(default=None)):
    if not id or not id.strip():
        return JSONResponse(
            status_code=400, content={"valid": False, "error": "Missing ID"}
        )

    normalized = id.strip().upper()
    # ID format: CB-HACK-2026-XXXXXX where XXXXXX is the first 6 chars of the
    # referenceNumber (or _id fallback).
    parts = normalized.split("-")
    if len(parts) < 4:
        return JSONResponse(status_code=200, content={"valid": False})
    short_id = "-".join(parts[3:])

    collection = contest_registrations_collection()
    cursor = collection.find(
        {}, {"fullName": 1, "referenceNumber": 1, "_id": 1, "isWinner": 1}
    )
    async for app in cursor:
        ref = app.get("referenceNumber") or str(app.get("_id")) or "1109"
        if ref[:6].upper() == short_id:
            return JSONResponse(
                status_code=200,
                content={
                    "valid": True,
                    "name": app.get("fullName") or "Hackathon Participant",
                    "isWinner": bool(app.get("isWinner")),
                },
            )

    return JSONResponse(status_code=200, content={"valid": False})
