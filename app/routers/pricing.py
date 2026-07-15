"""Public rate card."""
from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..pricing import rate_card

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("")
async def get_pricing():
    return {
        "status": True,
        "currency": get_settings().currency,
        "data": rate_card(),
    }
