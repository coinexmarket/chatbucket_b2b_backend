"""Request bodies for the operator notification endpoints."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from pydantic.alias_generators import to_camel


class _Broadcast(BaseModel):
    """What every fan-out shares: a rehearsal switch and a safety catch."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    # Send to this one address instead of the customer base. Nothing is
    # recorded as sent, so the real broadcast afterwards still reaches
    # everybody. Always do this first — an email is not recallable.
    test_email: EmailStr | None = Field(default=None)
    # Required to reach real customers. Not a formality: the difference between
    # a preview and mailing the entire base is one field, and it should be one
    # somebody typed on purpose.
    confirm: bool = Field(default=False)

    @model_validator(mode="after")
    def _require_confirmation(self):
        if not self.test_email and not self.confirm:
            raise ValueError(
                "Set testEmail to preview, or confirm=true to send to customers."
            )
        return self


class AnnouncementRequest(_Broadcast):
    subject: str = Field(min_length=1, max_length=200)
    headline: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    highlights: list[str] = Field(default_factory=list, max_length=6)
    quote: str = Field(default="", max_length=600)
    quote_author: str = Field(default="", max_length=120)
    category: str = Field(default="Announcement", max_length=60)
    # The hero band at the top. Defaults to the headline and summary, which is
    # usually right and always better than shipping the design's placeholder.
    hero_title: str | None = Field(default=None, max_length=120)
    hero_subtitle: str | None = Field(default=None, max_length=400)
    # Quoted back by anyone who replies. Generated when omitted.
    reference_id: str | None = Field(default=None, max_length=40)
    # Announcements are marketing, so they go to confirmed addresses only
    # unless someone deliberately says otherwise.
    verified_only: bool = Field(default=True)


class MaintenanceRequest(_Broadcast):
    subject: str = Field(default="Scheduled maintenance", min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime
    maintenance_type: str = Field(default="Scheduled Maintenance", max_length=80)
    reference_id: str | None = Field(default=None, max_length=40)
    # Unlike an announcement: this is service information, and an account with
    # an unconfirmed address can still be calling the API during the window.
    verified_only: bool = Field(default=False)

    @model_validator(mode="after")
    def _check_window(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("endsAt must be after startsAt.")
        return self


class MonthlyReportRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    # Any instant inside the month to report on. Omitted means the month that
    # has just ended, which is what a run on the 1st wants.
    month: datetime | None = Field(default=None)
