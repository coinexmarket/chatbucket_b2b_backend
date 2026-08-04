"""Helpers to turn raw Mongo documents into JSON the frontend understands.

The Next.js app consumes documents with:
* ``_id`` as a string (Mongo returns an ``ObjectId``),
* ``createdAt`` / ``updatedAt`` as ISO date strings (Mongo stores ``datetime``).

These helpers normalise those two concerns and provide the field projection
for the "overview" shape (``IBlogOverview`` in the frontend types).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from bson import ObjectId
from bson.decimal128 import Decimal128

from . import money

# Fields the frontend's IBlogOverview intentionally omits from the full IBlog.
# Used to build lightweight list responses (recent / related / featured / c-blogs).
_OVERVIEW_OMIT = {
    "body",
    "meta_title",
    "meta_keywords",
    "author",
    "section_ids",
    "og_image",
    "og_title",
    "og_description",
    "tags",
}


def _jsonify_value(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        # Money is stored exactly (see money.py); JSON can only carry a float.
        return money.to_json(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonify_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonify_value(v) for k, v in value.items()}
    return value


def iso(value):
    """ISO-8601 string for a datetime, or the value unchanged if it is not one.

    Mongo returns datetimes, but a document written by an older code path (or
    a mock) may hold a string already. Rendering either without a branch at
    every call site keeps the serializers readable.
    """
    return value.isoformat() if hasattr(value, "isoformat") else value


def serialize_doc(doc: dict | None) -> dict | None:
    """Convert a single Mongo document to a JSON-safe dict (``_id`` -> str)."""
    if doc is None:
        return None
    return {k: _jsonify_value(v) for k, v in doc.items()}


def serialize_docs(docs: Iterable[dict]) -> list[dict]:
    return [serialize_doc(d) for d in docs]


def to_overview(doc: dict | None) -> dict | None:
    """Serialize a blog and drop the heavy fields the list views never use."""
    full = serialize_doc(doc)
    if full is None:
        return None
    return {k: v for k, v in full.items() if k not in _OVERVIEW_OMIT}


def to_overviews(docs: Iterable[dict]) -> list[dict]:
    return [to_overview(d) for d in docs]


# Fields that must never leave the server on a user object.
_USER_SECRET_FIELDS = {
    "password_hash",
    "reset_token_hash",
    "reset_token_expires",
    "_api_key_id",
    "_api_key_project_id",
    "verification_token_hash",
    "verification_token_expires",
}


def public_user(doc: dict | None) -> dict | None:
    """Serialize a user document, stripping secret fields."""
    full = serialize_doc(doc)
    if full is None:
        return None
    return {k: v for k, v in full.items() if k not in _USER_SECRET_FIELDS}
