"""Plan limits — backs the dashboard's Limits page.

    GET /limits   the caller's plan, credit balance and per-API rate limits
    GET /limits/plans   the public plan catalogue (no auth)

Every API key on an account shares these limits, so they are reported per
service rather than per key.

Limits are enforced on `POST /usage` when `ENFORCE_PLAN_RATE_LIMITS` is on
(the default), counted per (account, service) in Mongo so the ceiling holds
across worker processes — an in-process counter would give a customer N times
the limit on an N-worker deployment. `GET /limits` reports which is in effect
via `enforced`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import credits, money
from ..config import get_settings
from ..deps import get_current_user
from ..plans import get_plan, plan_catalogue
from ..pricing import SERVICES

router = APIRouter(prefix="/limits", tags=["limits"])


@router.get("/plans")
async def list_plans():
    """The plan catalogue. Public, like `/pricing`."""
    return {"status": True, "data": plan_catalogue()}


@router.get("")
async def get_limits(user: dict = Depends(get_current_user)):
    plan = get_plan(user.get("plan"))
    balance = await credits.balance_units(user["_id"])

    return {
        "status": True,
        "data": {
            "plan": plan.key,
            "plan_label": plan.label,
            "support": plan.support,
            "best_for": plan.best_for,
            "credits": money.to_json(credits.from_units(balance)),
            "requests_per_minute": plan.requests_per_minute,
            "concurrency": plan.concurrency,
            # True when the limits below are actually applied to `POST /usage`.
            "enforced": get_settings().enforce_plan_rate_limits,
            "limits": [
                {
                    "service": s.key,
                    "label": s.label,
                    "unit": s.unit,
                    "requests_per_minute": plan.requests_per_minute,
                    "concurrency": plan.concurrency,
                }
                for s in SERVICES.values()
            ],
        },
    }
