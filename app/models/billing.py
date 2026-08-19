"""Request bodies for credits and billing."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

# GSTIN: 2-digit state code, 10-char PAN, entity number, 'Z', checksum.
_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


class TopUpRequest(BaseModel):
    """Start a top-up: either a named pack, or a custom rupee amount.

    Exactly one of the two. A pack carries bonus credits and moves the account
    onto that tier's rate limits; a custom amount is a plain 1 credit per rupee
    with no bonus and no tier change.
    """

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    plan: str | None = Field(default=None, description="Pack key: pro | business.")
    amount_inr: float | None = Field(
        default=None, gt=0, le=10_000_000, description="Custom top-up amount in INR."
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> TopUpRequest:
        if (self.plan is None) == (self.amount_inr is None):
            raise ValueError("Provide exactly one of 'plan' or 'amount_inr'.")
        return self


class AutoRechargeRequest(BaseModel):
    """Configure automatic top-up when the balance runs low."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    enabled: bool
    threshold_credits: float | None = Field(default=None, ge=0)
    amount_inr: float | None = Field(default=None, gt=0, le=10_000_000)

    @model_validator(mode="after")
    def _require_settings_when_enabled(self) -> AutoRechargeRequest:
        if self.enabled and (self.threshold_credits is None or self.amount_inr is None):
            raise ValueError(
                "threshold_credits and amount_inr are both required when "
                "auto-recharge is enabled."
            )
        return self


class PaymentConfirmation(BaseModel):
    """What the gateway webhook reports when a top-up is paid."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    provider_payment_id: str = Field(min_length=1, max_length=200)
    # Shown in the billing history, e.g. "Visa •••• 4242" or "UPI".
    method: str | None = Field(default=None, max_length=100)
    # When the gateway issues its own GST-compliant invoice, record its
    # reference so the dashboard can link to the authoritative document
    # instead of this service's minimal one.
    provider_invoice_id: str | None = Field(default=None, max_length=200)
    provider_invoice_url: str | None = Field(default=None, max_length=500)


class CheckoutCallback(BaseModel):
    """What the gateway's checkout widget hands back to the browser on success.

    Every field is untrusted — it arrives via the customer's page — so the
    signature is verified server-side before anything is credited.

    The field names are the widget's own and are echoed by the dashboard
    verbatim, so they stay as the gateway spells them even though nothing else
    here names it.
    """

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    razorpay_order_id: str = Field(min_length=1, max_length=200)
    razorpay_payment_id: str = Field(min_length=1, max_length=200)
    razorpay_signature: str = Field(min_length=1, max_length=200)


class BillingDetailsRequest(BaseModel):
    """The customer's invoicing identity.

    Separate from the profile because these are the details that get frozen
    onto an invoice, and they are the company's, not the person's.
    """

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    legal_name: str = Field(min_length=1, max_length=200)
    gstin: str | None = Field(default=None, max_length=15)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    # ISO 3166-1 alpha-2. Defaults to India, where the rate card is priced.
    country: str = Field(default="IN", min_length=2, max_length=2)

    @field_validator("gstin")
    @classmethod
    def _check_gstin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper().replace(" ", "")
        if not cleaned:
            return None
        if not _GSTIN.match(cleaned):
            raise ValueError(
                "GSTIN must be 15 characters, e.g. 29ABCDE1234F1Z5."
            )
        return cleaned

    @field_validator("country")
    @classmethod
    def _upper_country(cls, value: str) -> str:
        return value.strip().upper()
