"""Request body models for the write endpoints.

Read endpoints return blog documents straight from Mongo (see
``serialization.py``); only the POST bodies need validation, so those live
here. Field names mirror exactly what the frontend sends.
"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class SubscriptionRequest(BaseModel):
    """Body for POST /subscriptions/v1/notify-app-launch."""

    email: EmailStr


class ContestRegistrationRequest(BaseModel):
    """Body for POST /api/register (matches the register form's formData)."""

    fullName: str = Field(min_length=1)
    email: EmailStr
    mobileNumber: str = Field(min_length=1)
    course: str = ""
    useTranslationApp: str = ""
    dailyFeature: str = ""
    b2bIndustry: str = ""
    consent: bool = False
