"""MongoDB access layer — the single place that talks to Mongo.

This module isolates all database concerns from the rest of the application:

* one shared async client (Motor) created on startup, closed on shutdown;
* *separate* logical databases — B2B accounts/usage, blog content, and contest
  data — exposed through small accessor functions so routers never hard-code
  database names;
* the connection is established lazily on ``connect()`` (called from the app
  lifespan), so importing this module never requires a live database.

Keeping this seam here means the collections, database names, or even the
driver can change without touching any router code.
"""
from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import get_settings


class _Mongo:
    """Holds the process-wide Motor client and cached database handles."""

    client: AsyncIOMotorClient | None = None
    b2b_db: AsyncIOMotorDatabase | None = None
    blog_db: AsyncIOMotorDatabase | None = None
    contest_db: AsyncIOMotorDatabase | None = None
    # True once every index in `ensure_indexes` exists. Correctness depends on
    # this: `register` relies on the unique email index to reject duplicates.
    indexes_ready: bool = False


_mongo = _Mongo()


async def connect() -> None:
    """Create the Motor client and resolve the logical databases.

    Called once from the FastAPI lifespan on startup. Safe to call again; it
    is a no-op if a client already exists.
    """
    if _mongo.client is not None:
        return

    settings = get_settings()
    _mongo.client = AsyncIOMotorClient(
        settings.mongodb_uri,
        uuidRepresentation="standard",
        tz_aware=True,
    )
    _mongo.b2b_db = _mongo.client[settings.b2b_db_name]
    _mongo.blog_db = _mongo.client[settings.blog_db_name]
    _mongo.contest_db = _mongo.client[settings.contest_db_name]


async def ensure_indexes() -> None:
    """Create the indexes the app relies on. Idempotent.

    Raises if the database is unreachable; the caller is expected to retry
    (see `_ensure_indexes_forever` in `main.py`). `indexes_ready` stays False
    until every index below exists, so callers can tell the difference between
    "enforced by Mongo" and "not yet".
    """
    await users_collection().create_index("email", unique=True)
    await api_keys_collection().create_index("key_hash", unique=True)
    await api_keys_collection().create_index("user_id")
    await usage_collection().create_index([("user_id", 1), ("created_at", -1)])
    await usage_collection().create_index("service")
    # Makes `POST /usage` retries safe: one usage record per (customer, key).
    # Scoped per user so two customers can pick the same key, and partial so
    # the many records sent without a key never collide with each other.
    await usage_collection().create_index(
        [("user_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    await subscriptions_collection().create_index("email", unique=True)
    _mongo.indexes_ready = True


def indexes_ready() -> bool:
    """True when the indexes the app depends on are known to exist."""
    return _mongo.indexes_ready


async def disconnect() -> None:
    """Close the Motor client on shutdown."""
    if _mongo.client is not None:
        _mongo.client.close()
        _mongo.client = None
        _mongo.b2b_db = None
        _mongo.blog_db = None
        _mongo.contest_db = None
        _mongo.indexes_ready = False


async def ping() -> bool:
    """Return True if the server answers a ``ping`` command."""
    if _mongo.client is None:
        return False
    try:
        await _mongo.client.admin.command("ping")
        return True
    except Exception:
        return False


def get_b2b_db() -> AsyncIOMotorDatabase:
    if _mongo.b2b_db is None:
        raise RuntimeError("MongoDB not connected. Did startup run?")
    return _mongo.b2b_db


def get_blog_db() -> AsyncIOMotorDatabase:
    if _mongo.blog_db is None:
        raise RuntimeError("MongoDB not connected. Did startup run?")
    return _mongo.blog_db


def get_contest_db() -> AsyncIOMotorDatabase:
    if _mongo.contest_db is None:
        raise RuntimeError("MongoDB not connected. Did startup run?")
    return _mongo.contest_db


# --- Collection accessors -------------------------------------------------
# Named so the intent is obvious at every call site.

def users_collection():
    return get_b2b_db()["users"]


def api_keys_collection():
    return get_b2b_db()["api_keys"]


def usage_collection():
    return get_b2b_db()["usage"]


def blogs_collection():
    return get_blog_db()["blogs"]


def categories_collection():
    return get_blog_db()["categories"]


def subscriptions_collection():
    return get_blog_db()["subscriptions"]


def contest_registrations_collection():
    # Matches the collection the existing Next.js /api routes write to.
    return get_contest_db()["contest_registrations"]
