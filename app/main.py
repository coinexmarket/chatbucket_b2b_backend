"""FastAPI application entrypoint.

Wires together the config, the (separate) MongoDB layer, CORS, and all routers.
Run locally with::

    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import database
from .config import get_settings
from .routers import (
    account,
    api_keys,
    auth,
    billing,
    blogs,
    contest,
    demo,
    limits,
    pricing,
    profile,
    projects,
    subscriptions,
    usage,
    vendors,
)
from .routers import (
    status as status_router,
)
from .security import warm_password_hasher

logger = logging.getLogger("chatbucket_b2b")

_INDEX_RETRY_MAX_DELAY = 60.0


async def _ensure_indexes_forever() -> None:
    """Retry index creation until it succeeds, with capped exponential backoff.

    Startup deliberately survives a briefly-unreachable Mongo, but a one-shot
    attempt would leave the process serving for its whole life without the
    unique email index that `register` depends on to reject duplicate accounts.
    Retrying makes the outage transient instead of permanent.
    """
    delay = 1.0
    while True:
        await asyncio.sleep(delay)
        try:
            await database.ensure_indexes()
            logger.info("ensure_indexes succeeded on retry; indexes are ready")
            return
        except Exception as exc:
            logger.error(
                "ensure_indexes retry failed, next attempt in %.0fs: %s", delay, exc
            )
            delay = min(delay * 2, _INDEX_RETRY_MAX_DELAY)


async def _probe_services_forever() -> None:
    """Poll each configured health URL and record the result.

    Only runs when `STATUS_PROBE_URLS` is set, so a deployment that reports via
    heartbeats instead pays nothing for this. Uses the stdlib in a worker
    thread rather than adding an HTTP client dependency for one background job.
    """
    import urllib.error
    import urllib.request

    from starlette.concurrency import run_in_threadpool

    from . import status as service_status

    settings = get_settings()
    probes = settings.status_probe_map

    def check(url: str) -> str:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return (
                    service_status.OPERATIONAL
                    if 200 <= response.status < 300
                    else service_status.DEGRADED
                )
        except urllib.error.HTTPError as exc:
            # It answered, just not happily — degraded rather than down.
            return service_status.DEGRADED if exc.code < 500 else service_status.DOWN
        except Exception:
            return service_status.DOWN

    while True:
        for key, url in probes.items():
            try:
                result = await run_in_threadpool(check, url)
                await service_status.record(key, result, service_status.SOURCE_PROBE)
            except Exception as exc:
                logger.error("status probe for %s failed: %s", key, exc)
        await asyncio.sleep(settings.status_probe_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the Mongo connection on startup, close it on shutdown.
    await database.connect()

    # Build the login timing-defence hash now rather than on the event loop
    # during the first login that misses.
    warm_password_hasher()

    # Stated at boot so "no reset emails arrived" is one log line to diagnose
    # rather than a guess about configuration.
    logger.info("email backend: %s", settings.resolved_email_backend)

    # Try inline first so a healthy boot has its indexes before the first
    # request; fall back to retrying in the background rather than giving up.
    retry_task: asyncio.Task | None = None
    try:
        await database.ensure_indexes()
    except Exception as exc:
        logger.error("ensure_indexes failed at startup, retrying in background: %s", exc)
        retry_task = asyncio.create_task(_ensure_indexes_forever())

    probe_task: asyncio.Task | None = None
    if settings.status_probe_map:
        logger.info("status probing %d service(s)", len(settings.status_probe_map))
        probe_task = asyncio.create_task(_probe_services_forever())

    try:
        yield
    finally:
        for task in (retry_task, probe_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await database.disconnect()


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "ChatBucket B2B API platform: accounts, API keys, and usage-based "
        "billing for STT, TTS, translation, chat and voice agents. Also serves "
        "the chatbucket-web blog, subscription and contest endpoints."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- B2B platform ---------------------------------------------------------
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(account.router)
app.include_router(api_keys.router)
app.include_router(usage.router)
app.include_router(pricing.router)
app.include_router(limits.router)
app.include_router(billing.router)
app.include_router(projects.router)
app.include_router(status_router.router)
# Operator-only: what our upstream suppliers cost us, not what we charge.
app.include_router(vendors.router)

# --- chatbucket-web site endpoints ---------------------------------------
app.include_router(blogs.router)
app.include_router(subscriptions.router)
app.include_router(contest.router)
# Called from the marketing site, but the leads land in the B2B database.
app.include_router(demo.router)


@app.get("/", tags=["health"])
async def root():
    return {"status": True, "service": settings.app_name, "version": "1.0.0"}


@app.get("/health", tags=["health"])
async def health():
    """Liveness + database connectivity probe.

    Returns 503 when Mongo is unreachable: load balancers and orchestrators
    route on the status code, so a 200 carrying `"database": "down"` would keep
    a broken instance in rotation.
    """
    db_ok = await database.ping()
    return JSONResponse(
        status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": db_ok,
            "service": settings.app_name,
            "database": "up" if db_ok else "down",
            # Informational: a lasting "pending" means the retry is stuck and
            # uniqueness is running on the fallback check in `register`.
            "indexes": "ready" if database.indexes_ready() else "pending",
        },
    )
