"""Request body models for the write endpoints.

Read endpoints return blog documents straight from Mongo (see
``serialization.py``); only the POST bodies need validation, so those live
here. Field names mirror exactly what the frontend sends.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pydantic.alias_generators import to_camel


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


# --- Demo requests ---------------------------------------------------------
# The "Let's get your demo started" modal, which has a Personal/Business
# toggle. Both tabs share name/email/mobile and the marketing-consent
# checkbox; each adds its own fields. Modelled as a discriminated union on
# `type` so Pydantic enforces "company_name is required, but only for
# business" — a single flat model with everything optional would accept a
# business lead with no company on it.


class _DemoRequestBase(BaseModel):
    # Accept camelCase (what the React form holds) as well as the snake_case
    # this API uses elsewhere, so the two sides can't drift over field naming.
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    # The "get updates about events, webinars…" checkbox. Unticked by default,
    # so an absent field must mean no consent, never yes.
    subscribe_updates: bool = False


class PersonalDemoRequest(_DemoRequestBase):
    type: Literal["personal"]
    # The Personal tab does not ask for a phone number — it collects an email
    # and a free-text box — so requiring one here would reject every lead the
    # form can actually produce. Optional, not absent, in case a later form or
    # an API caller has one.
    mobile: str | None = Field(default=None, max_length=32)
    how_did_you_hear: str | None = Field(default=None, max_length=2000)


class BusinessDemoRequest(_DemoRequestBase):
    type: Literal["business"]
    # Required here: sales calls business leads, and the Business tab asks.
    mobile: str = Field(min_length=4, max_length=32)
    company_name: str = Field(min_length=1, max_length=200)
    company_details: str | None = Field(default=None, max_length=2000)


# The free-text boxes are optional (neither is marked required in the form);
# company_name is required because a business demo without one is not
# actionable for sales.
DemoRequestBody = Annotated[
    PersonalDemoRequest | BusinessDemoRequest, Field(discriminator="type")
]
