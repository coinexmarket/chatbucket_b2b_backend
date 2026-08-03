"""Request bodies for account closure."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    # Re-confirming the password means a stolen access token alone cannot
    # close an account — this is the one action with no undo.
    password: str = Field(min_length=1)
