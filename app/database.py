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

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import get_settings

logger = logging.getLogger("chatbucket_b2b.database")


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
    # `reset-password` looks a user up by this hash alone; without an index
    # every attempt is a full collection scan. Partial because only the handful
    # of users with a reset in flight carry the field at all.
    await users_collection().create_index(
        "reset_token_hash",
        partialFilterExpression={"reset_token_hash": {"$exists": True}},
    )
    # A mobile number identifies one account, the same way an email does.
    # Without this, `verify-phone` cannot tell which account a texted code
    # belongs to, and the signup bonus is farmable by re-registering the same
    # number against new addresses. Partial, because accounts predating the
    # signup form carry no phone at all and must not collide on null.
    #
    # Tolerated rather than fatal: a database that already contains duplicate
    # numbers would otherwise abort this whole function and leave every later
    # index uncreated. The explicit check in `register` covers the gap, exactly
    # as it does while the email index is missing.
    try:
        await users_collection().create_index(
            "phone",
            unique=True,
            partialFilterExpression={"phone": {"$type": "string"}},
        )
    except Exception as exc:
        logger.error(
            "could not create the unique phone index (duplicate numbers in the "
            "data?): %s. Registration falls back to an explicit check; resolve "
            "the duplicates and restart to enforce it in the database.", exc
        )
    await api_keys_collection().create_index("key_hash", unique=True)
    await api_keys_collection().create_index("user_id")
    await usage_collection().create_index([("user_id", 1), ("created_at", -1)])
    await usage_collection().create_index("service")
    # Backs the per-model breakdown and the `?model=` filter. Partial because
    # records from callers that never report a model would otherwise all index
    # a null.
    await usage_collection().create_index(
        [("user_id", 1), ("model_key", 1)],
        partialFilterExpression={"model_key": {"$type": "string"}},
    )
    # Makes `POST /usage` retries safe: one usage record per (customer, key).
    # Scoped per user so two customers can pick the same key, and partial so
    # the many records sent without a key never collide with each other.
    await usage_collection().create_index(
        [("user_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    await subscriptions_collection().create_index("email", unique=True)
    # One credit account per user; unique so a race on first touch cannot
    # create two balances for the same customer.
    await credit_accounts_collection().create_index("user_id", unique=True)
    await credit_ledger_collection().create_index([("user_id", 1), ("created_at", -1)])
    await payments_collection().create_index([("user_id", 1), ("created_at", -1)])
    # Set by the gateway webhook; unique so a redelivered callback cannot
    # credit the same payment twice.
    # Webhooks arrive keyed by order id, so it must be indexed and unique —
    # two local payments sharing one Razorpay order would be ambiguous.
    await payments_collection().create_index(
        "provider_order_id",
        unique=True,
        partialFilterExpression={"provider_order_id": {"$exists": True}},
    )
    await payments_collection().create_index(
        "provider_payment_id",
        unique=True,
        partialFilterExpression={"provider_payment_id": {"$exists": True}},
    )
    await service_status_collection().create_index("service", unique=True)
    await service_status_days_collection().create_index(
        [("service", 1), ("day", 1)], unique=True
    )
    # Project names are unique per customer (on the case-folded key), so a
    # second "Production" is rejected rather than creating a confusing twin.
    await projects_collection().create_index(
        [("user_id", 1), ("name_key", 1)], unique=True
    )
    await usage_collection().create_index(
        [("user_id", 1), ("project_id", 1)],
        partialFilterExpression={"project_id": {"$type": "string"}},
    )
    # Mongo removes each rate-limit window itself once it passes, so no sweep
    # of ours is needed and a restart cannot leave stale counters behind.
    await rate_limits_collection().create_index("expires_at", expireAfterSeconds=0)
    # Same for expired sessions.
    await refresh_tokens_collection().create_index("token_hash", unique=True)
    await refresh_tokens_collection().create_index("user_id")
    await refresh_tokens_collection().create_index("expires_at", expireAfterSeconds=0)
    # Invoice numbers must be unique, and one payment may only ever produce one
    # invoice — both enforced by the database, not just by the code path.
    await invoices_collection().create_index("invoice_number", unique=True)
    await invoices_collection().create_index("payment_id", unique=True)
    await invoices_collection().create_index([("user_id", 1), ("issued_at", -1)])
    # Not unique: a repeat demo request is a real sales lead, not an error.
    await demo_requests_collection().create_index([("created_at", -1)])
    await demo_requests_collection().create_index("email")
    # What makes a lifecycle email send-once. `key` distinguishes instances of
    # the same kind — the month for a report, the window id for a reminder —
    # so "monthly report for July" and "for August" are two different sends
    # while a re-run of either is a duplicate the database refuses.
    await notifications_collection().create_index(
        [("user_id", 1), ("kind", 1), ("key", 1)], unique=True
    )
    await notifications_collection().create_index([("sent_at", -1)])
    # The scheduler's mutual exclusion. Unique on (job, period), so of the two
    # workers that wake at the same moment, exactly one insert succeeds and the
    # other reads its own duplicate-key error as "somebody else has this".
    await job_runs_collection().create_index([("job", 1), ("period", 1)], unique=True)

    # One live code per number, so a resend replaces the previous code rather
    # than leaving two valid ones with one attempt counter between them.
    await phone_verifications_collection().create_index("phone", unique=True)
    # These records are worthless once spent, and they hold a code hash for a
    # number belonging to somebody who never became a customer. Mongo expires
    # them on `purge_at` so neither this app nor an operator has to remember to.
    await phone_verifications_collection().create_index(
        "purge_at", expireAfterSeconds=0
    )
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


def credit_accounts_collection():
    # One document per user: the authoritative credit balance.
    return get_b2b_db()["credit_accounts"]


def credit_ledger_collection():
    # Append-only history of every credit movement.
    return get_b2b_db()["credit_ledger"]


def payments_collection():
    # Top-up orders, from `pending` through `paid`.
    return get_b2b_db()["payments"]


def invoices_collection():
    # One immutable invoice per paid top-up.
    return get_b2b_db()["invoices"]


def service_status_collection():
    # Current status, one document per system.
    return get_b2b_db()["service_status"]


def service_status_days_collection():
    # Daily rollup backing the 90-day uptime strip.
    return get_b2b_db()["service_status_days"]


def projects_collection():
    # Customer-defined grouping for API keys and the usage they generate.
    return get_b2b_db()["projects"]


def rate_limits_collection():
    # Fixed-window request counters, expired by a TTL index.
    return get_b2b_db()["rate_limits"]


def refresh_tokens_collection():
    # Long-lived session tokens; only their hashes are stored.
    return get_b2b_db()["refresh_tokens"]


def counters_collection():
    # Atomic sequences (invoice numbering). One document per counter.
    return get_b2b_db()["counters"]


def demo_requests_collection():
    # Sales leads, so they live with the B2B data rather than the site content.
    return get_b2b_db()["demo_requests"]


def notifications_collection():
    # One document per lifecycle email actually sent, so a job that runs twice
    # (a retry, an overlapping cron, a manual re-run) does not mail the same
    # customer the same thing twice.
    return get_b2b_db()["notifications"]


def job_runs_collection():
    # One document per scheduled job per period. The app runs more than one
    # worker, each with its own scheduler loop, so this is what stops both of
    # them running the same monthly report — and it doubles as the record of
    # when each job last ran.
    return get_b2b_db()["job_runs"]


def phone_verifications_collection():
    # A mobile number proven **before** an account exists for it. The signup
    # form asks for the number, texts a code and checks it while the customer is
    # still filling the form in, so at that moment there is no user document to
    # hang the code off — hence a collection keyed by the number itself.
    #
    # Deliberately separate from `users`: these are unauthenticated, cheap to
    # create and expire quickly, and mixing them into `users` would mean
    # half-real accounts that every other query has to learn to skip.
    return get_b2b_db()["phone_verifications"]


def blogs_collection():
    return get_blog_db()["blogs"]


def categories_collection():
    return get_blog_db()["categories"]


def subscriptions_collection():
    return get_blog_db()["subscriptions"]


def contest_registrations_collection():
    # Matches the collection the existing Next.js /api routes write to.
    return get_contest_db()["contest_registrations"]
