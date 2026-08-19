"""Request bodies for authentication and profile endpoints."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel

# E.164: '+', a country code that cannot start with 0, and 15 digits at most.
# Stored in this one canonical form so an SMS/WhatsApp provider can dial it
# without the app having to guess a country later.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_PHONE_SEPARATORS = re.compile(r"[\s\-().]")


def normalize_phone(value: str) -> str:
    """Strip formatting and validate as E.164, or raise with what to fix.

    The signup form has a country selector, so the number arrives with a dial
    code already; what varies is how the user typed the rest. "+91 98765-43210"
    and "+919876543210" are the same number and must not become two different
    stored values.
    """
    cleaned = _PHONE_SEPARATORS.sub("", value.strip())
    if not cleaned.startswith("+"):
        raise ValueError(
            "Mobile number must start with a country code, e.g. +919876543210."
        )
    if not _E164.match(cleaned):
        raise ValueError(
            "Mobile number must be 8-15 digits in international format, "
            "e.g. +919876543210."
        )
    return cleaned


class RegisterRequest(BaseModel):
    # `extra="forbid"` because the default is to *drop* unknown keys: a form
    # posting a field the API does not model would get a 201 and lose it
    # silently. Failing loudly turns that into an obvious integration error.
    # camelCase is accepted alongside snake_case, as on the demo endpoints.
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    mobile: str = Field(description="International format, e.g. +919876543210.")
    company: str | None = Field(default=None, max_length=200)
    # Free text rather than an enum: the signup form owns the option list, so
    # it can add or reword a choice without a backend deploy.
    how_did_you_hear: str | None = Field(default=None, max_length=200)
    # Required and must be true. Recorded as a timestamp on the user (see
    # `routers/auth.py`) — a consent record you cannot date is not a record.
    accept_terms: bool = Field(description="Must be true to create an account.")

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("accept_terms")
    @classmethod
    def _check_terms(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "You must accept the Terms & Conditions to create an account."
            )
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class ProfileUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    company: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        # Same rule as registration: `phone` is one field on the user document,
        # so editing it must not be a way to store a format registration would
        # have rejected.
        return normalize_phone(value) if value is not None else value


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    token: str = Field(min_length=1)


class VerifyPhoneRequest(BaseModel):
    """The six digits texted to a mobile number, plus the number itself.

    Takes the number rather than a session for the same reason the email OTP
    takes an address: someone verifying on a phone may not be signed in. The
    number must be in the same E.164 form it was registered with, so the
    lookup is exact.
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    mobile: str = Field(description='International format, e.g. +919876543210.')
    code: str = Field(min_length=6, max_length=6, pattern=r'^\d{6}$')

    @field_validator('mobile')
    @classmethod
    def _check_mobile(cls, value: str) -> str:
        return normalize_phone(value)


class ResendPhoneCodeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    mobile: str = Field(description='International format, e.g. +919876543210.')

    @field_validator('mobile')
    @classmethod
    def _check_mobile(cls, value: str) -> str:
        return normalize_phone(value)


class VerifyEmailOtpRequest(BaseModel):
    """The six digits from the verification email, plus who they belong to.

    The address is required because this endpoint is unauthenticated, like the
    token one: someone verifying from their phone has no session. Six digits
    alone would be a code anybody's account might match.
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RefreshRequest(BaseModel):
    # camelCase accepted alongside snake_case, as on the other client-facing
    # bodies — the dashboard should not have to remember which is which.
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # Omit to sign out only the current access token's session; pass
    # all_sessions to sign out everywhere.
    refresh_token: str | None = Field(default=None)
    all_sessions: bool = Field(default=False)


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    name: str = Field(default="default", min_length=1, max_length=80)
    # Optional, matching the "Select Project (Optional)" field on the modal.
    project_id: str | None = Field(default=None)


class ApiKeyRenameRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # Required, unlike on create: a rename with no name is a mistake, not a
    # request to fall back to "default".
    name: str = Field(min_length=1, max_length=80)
    # Pass an id to move the key, or "" to unassign it. Omitting leaves the
    # project alone — a rename should not silently detach the key.
    project_id: str | None = Field(default=None)
