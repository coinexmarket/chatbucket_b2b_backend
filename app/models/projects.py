"""Request bodies for projects."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Case-folded key for the per-customer uniqueness index.

    Folds case and spacing only, so "Production" and "production " collide but
    two genuinely different names never do — the same rule used for models.
    """
    return _WHITESPACE.sub(" ", name.strip()).lower()


class ProjectRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
