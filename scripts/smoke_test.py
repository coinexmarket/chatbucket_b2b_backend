"""End-to-end smoke test driving every endpoint through the real FastAPI app.

Uses an in-memory async Mongo (mongomock-motor) wired into the database layer,
so no real MongoDB is required. Run:  python -m scripts.smoke_test
Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

# Ignore any local `.env`: the suite must assert the same thing on every
# machine, not whatever the developer running it happens to have configured.
os.environ["CHATBUCKET_IGNORE_DOTENV"] = "1"

# Everything the assertions depend on is pinned here rather than left to the
# defaults in config.py. Set before `app` is imported, because the settings
# object is built (and cached) at import time.
os.environ["EMAIL_BACKEND"] = "memory"  # capture mail instead of sending it
os.environ["SALES_EMAIL"] = "sales@chatbucket.chat"
os.environ["STATUS_WEBHOOK_SECRET"] = "test-status-secret"
os.environ["ENVIRONMENT"] = "development"
os.environ["RATE_LIMIT_ENABLED"] = "true"  # demo + contest asserts the 429s
os.environ["STATUS_STALE_AFTER_SECONDS"] = "300"  # silence reads `unknown`

from fastapi.testclient import TestClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import database  # noqa: E402
from app.email import outbox  # noqa: E402
from app.main import app  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wire_in_memory_db() -> None:
    """Point the database layer at an in-memory Mongo and seed data."""
    client = AsyncMongoMockClient()
    database._mongo.client = client  # type: ignore[attr-defined]
    database._mongo.blog_db = client["chatbucket"]  # type: ignore[attr-defined]
    database._mongo.contest_db = client["ChatBucketHackathon"]  # type: ignore
    # Demo requests are stored in the B2B database, so it has to be wired here
    # too — without it `ensure_indexes` fails at startup and the endpoint 500s.
    database._mongo.b2b_db = client["chatbucket_b2b"]  # type: ignore


PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    wire_in_memory_db()

    # Seed directly via the (mocked) collections, synchronously through the
    # mock's awaitables using the TestClient's event loop is overkill; instead
    # seed through the API where possible and via a tiny helper otherwise.
    with TestClient(app) as client:
        # --- health -----------------------------------------------------
        r = client.get("/health")
        check("GET /health 200", r.status_code == 200, r.text)

        # --- seed blogs/categories through the mock collections ---------
        import anyio

        async def seed():
            blogs = database.blogs_collection()
            cats = database.categories_collection()
            await cats.insert_many(
                [
                    {"name": "Product", "createdAt": _now()},
                    {"name": "Engineering", "createdAt": _now()},
                ]
            )
            await blogs.insert_many(
                [
                    {
                        "title": "Introducing ChatBucket",
                        "body": "<p>hi</p>",
                        "slug": "introducing-chatbucket",
                        "author": "Team",
                        "meta_title": "t",
                        "meta_desc": "spatial secure chat",
                        "meta_keywords": "k",
                        "category": "Product",
                        "sub_category": "Announcements",
                        "tags": ["launch"],
                        "featured_img": "img",
                        "og_image": "og",
                        "og_title": "og",
                        "og_description": "og",
                        "featured": True,
                        "faq": [{"question": "q", "answer": "a"}],
                        "createdAt": _now(),
                        "updatedAt": _now(),
                    },
                    {
                        "title": "Scaling chat",
                        "body": "<p>deep</p>",
                        "slug": "scaling-realtime-chat",
                        "author": "Eng",
                        "meta_title": "t",
                        "meta_desc": "architecture",
                        "meta_keywords": "k",
                        "category": "Engineering",
                        "tags": ["backend"],
                        "featured_img": "img",
                        "og_image": "og",
                        "og_title": "og",
                        "og_description": "og",
                        "featured": False,
                        "faq": [],
                        "createdAt": _now(),
                        "updatedAt": _now(),
                    },
                ]
            )

        import asyncio

        asyncio.new_event_loop().run_until_complete(seed())

        # --- blog list (sitemap reads body.blogs) -----------------------
        r = client.get("/v1/blogs")
        j = r.json()
        check("GET /v1/blogs status", r.status_code == 200 and j["status"] is True, r.text)
        check("GET /v1/blogs has 'blogs' key", isinstance(j.get("blogs"), list) and len(j["blogs"]) == 2)
        check("overview omits body", "body" not in j["blogs"][0], str(j["blogs"][0].keys()))
        check("overview _id is str", isinstance(j["blogs"][0]["_id"], str))

        # --- single blog ------------------------------------------------
        r = client.get("/v1/blogs/introducing-chatbucket")
        j = r.json()
        check("GET /v1/blogs/{slug} full body present", j["status"] and j["data"]["body"] == "<p>hi</p>", r.text)

        r = client.get("/v1/blogs/does-not-exist")
        check("GET missing slug -> 404 status:false", r.status_code == 404 and r.json()["status"] is False)

        # --- v2 category-scoped ----------------------------------------
        r = client.get("/v2/blogs/introducing-chatbucket?category=Product&sub_category=Announcements")
        check("GET /v2/blogs match", r.status_code == 200 and r.json()["data"]["slug"] == "introducing-chatbucket", r.text)
        r = client.get("/v2/blogs/introducing-chatbucket?category=WrongCat")
        check("GET /v2/blogs wrong cat -> 404", r.status_code == 404)

        # --- recent / featured / related / categories -------------------
        r = client.get("/v1/recent-blogs")
        check("GET /v1/recent-blogs full shape", r.json()["data"][0].get("body") is not None, r.text)
        r = client.get("/v1/featured-blogs")
        j = r.json()
        check("GET /v1/featured-blogs only featured", len(j["data"]) == 1 and j["data"][0]["slug"] == "introducing-chatbucket", r.text)
        r = client.get("/v1/related-blogs/Engineering")
        check("GET /v1/related-blogs/{cat}", len(r.json()["data"]) == 1, r.text)
        r = client.get("/v1/categories")
        j = r.json()
        check("GET /v1/categories has categories key", isinstance(j.get("categories"), list) and len(j["categories"]) == 2, r.text)

        # --- c-blogs filter + search -----------------------------------
        r = client.get("/v1/c-blogs?categories=Product")
        check("GET /v1/c-blogs by category", len(r.json()["blogs"]) == 1, r.text)
        r = client.get("/v1/c-blogs?text=architecture")
        check("GET /v1/c-blogs by text", len(r.json()["blogs"]) == 1 and r.json()["blogs"][0]["slug"] == "scaling-realtime-chat", r.text)

        # --- subscriptions ---------------------------------------------
        r = client.post("/subscriptions/v1/notify-app-launch", json={"email": "a@b.com"})
        check("POST notify-app-launch 201", r.status_code == 201, r.text)
        r = client.post("/subscriptions/v1/notify-app-launch", json={"email": "a@b.com"})
        check("POST notify duplicate 409 err_code", r.status_code == 409 and r.json()["err_code"] == 409, r.text)
        r = client.post("/subscriptions/v1/notify-app-launch", json={"email": "not-an-email"})
        check("POST notify invalid email 422", r.status_code == 422, r.text)

        # --- contest register + verify ---------------------------------
        r = client.post(
            "/api/register",
            json={
                "fullName": "Ada Lovelace",
                "email": "ada@x.com",
                "mobileNumber": "12345",
                "course": "CS",
                "useTranslationApp": "yes",
                "dailyFeature": "chat",
                "b2bIndustry": "edu",
                "consent": True,
            },
        )
        j = r.json()
        ref = j.get("data", {}).get("referenceNumber", "")
        check("POST /api/register success + ref", j.get("success") is True and ref.startswith("WX-"), r.text)

        short = ref[:6].upper()
        r = client.get(f"/api/verify?id=CB-HACK-2026-{short}")
        j = r.json()
        check("GET /api/verify valid match", j.get("valid") is True and j.get("name") == "Ada Lovelace", r.text)
        r = client.get("/api/verify?id=CB-HACK-2026-ZZZZZZ")
        check("GET /api/verify no match", r.json().get("valid") is False, r.text)
        r = client.get("/api/verify?id=")
        check("GET /api/verify missing id -> 400", r.status_code == 400, r.text)

        # --- demo requests (Personal / Business modal) ------------------
        outbox.clear()
        r = client.post(
            "/demo-requests",
            json={
                "type": "personal",
                "name": "  Grace Hopper ",
                "email": " Grace@Example.COM ",
                "mobile": "9876543210",
                "howDidYouHear": "A colleague",
                "subscribeUpdates": True,
            },
        )
        j = r.json()
        check("POST /demo-requests personal 201", r.status_code == 201 and j["data"]["type"] == "personal" and j["data"]["id"], r.text)

        r = client.post(
            "/demo-requests",
            json={
                "type": "business",
                "name": "Alan Turing",
                "email": "alan@acme.com",
                "mobile": "9876500000",
                "company_name": "Acme Global",
                "company_details": "500-seat contact centre",
            },
        )
        check("POST /demo-requests business 201 (snake_case)", r.status_code == 201 and r.json()["data"]["type"] == "business", r.text)

        # --- sales notification -----------------------------------------
        check("both demo requests notify sales", len(outbox) == 2, str(outbox))
        biz_mail = next((m for m in outbox if "Acme Global" in m["subject"]), {})
        check("notification goes to SALES_EMAIL", biz_mail.get("to") == "sales@chatbucket.chat", str(biz_mail))
        check("notification subject names the lead", biz_mail.get("subject") == "New demo request: Alan Turing (Acme Global)", str(biz_mail))
        body = biz_mail.get("body", "")
        check("notification carries contact details", "alan@acme.com" in body and "9876500000" in body, body)
        check("notification carries company details", "500-seat contact centre" in body, body)
        check("notification records consent state", "Wants product updates: no" in body, body)

        async def read_demo_requests():
            return await database.demo_requests_collection().find({}).to_list(length=None)

        stored = anyio.run(read_demo_requests)
        personal = next((d for d in stored if d["type"] == "personal"), None)
        business = next((d for d in stored if d["type"] == "business"), None)
        check("demo lead trims + lowercases email", personal is not None and personal["email"] == "grace@example.com" and personal["name"] == "Grace Hopper", str(personal))
        check("demo lead keeps consent + how-heard", personal is not None and personal["subscribe_updates"] is True and personal["how_did_you_hear"] == "A colleague", str(personal))
        check("business lead stores company", business is not None and business["company_name"] == "Acme Global", str(business))
        check("consent defaults to False when absent", business is not None and business["subscribe_updates"] is False, str(business))
        check("lead is queued as new", business is not None and business["status"] == "new", str(business))
        check("personal lead has no company field", personal is not None and "company_name" not in personal, str(personal))

        # Duplicates are allowed on purpose — a repeat request is a real lead.
        r = client.post(
            "/demo-requests",
            json={"type": "personal", "name": "Grace Hopper", "email": "grace@example.com", "mobile": "9876543210"},
        )
        check("duplicate demo request still 201", r.status_code == 201, r.text)

        # Business without a company must fail; the same body as personal is fine.
        r = client.post(
            "/demo-requests",
            json={"type": "business", "name": "No Co", "email": "n@x.com", "mobile": "9876543210"},
        )
        check("business without company -> 422", r.status_code == 422, r.text)
        r = client.post(
            "/demo-requests",
            json={"type": "enterprise", "name": "X", "email": "n@x.com", "mobile": "9876543210"},
        )
        check("unknown demo type -> 422", r.status_code == 422, r.text)
        r = client.post(
            "/demo-requests",
            json={"type": "personal", "name": "X", "email": "not-an-email", "mobile": "9876543210"},
        )
        check("demo invalid email -> 422", r.status_code == 422, r.text)
        r = client.post(
            "/demo-requests",
            json={"type": "personal", "name": "", "email": "n@x.com", "mobile": "9876543210"},
        )
        check("demo empty name -> 422", r.status_code == 422, r.text)

        # --- service status ---------------------------------------------
        r = client.get("/status")
        j = r.json()
        check("GET /status is public", r.status_code == 200, r.text)
        check("status lists all six systems", len(j["data"]) == 6, r.text)
        # Nothing has reported yet, so it must NOT claim everything is fine.
        check("unreported systems read unknown, not operational", all(s["status"] == "unknown" for s in j["data"]), str([s["status"] for s in j["data"]]))
        check("overall reflects that", j["overall"] == "unknown" and "unknown" in j["message"].lower(), r.text)
        check("history has 90 daily buckets", len(j["data"][0]["history"]) == 90, str(len(j["data"][0]["history"])))
        check("system carries its component count", any(s["service"] == "stt" and s["components"] == 6 for s in j["data"]), r.text)

        secret = {"X-Status-Secret": "test-status-secret"}
        r = client.post("/status/heartbeat", json={"service": "tts"})
        check("heartbeat without secret -> 401", r.status_code == 401, r.text)
        r = client.post("/status/heartbeat", headers={"X-Status-Secret": "wrong"}, json={"service": "tts"})
        check("heartbeat with wrong secret -> 401", r.status_code == 401, r.text)

        r = client.post("/status/heartbeat", headers=secret, json={"service": "tts"})
        check("heartbeat defaults to operational", r.status_code == 200 and r.json()["data"]["status"] == "operational", r.text)
        r = client.post("/status/heartbeat", headers=secret, json={"service": "nope"})
        check("heartbeat for unknown system -> 400", r.status_code == 400, r.text)
        r = client.post("/status/heartbeat", headers=secret, json={"service": "stt", "status": "sideways"})
        check("invalid status value -> 422", r.status_code == 422, r.text)

        r = client.put("/status/chat", headers=secret, json={"status": "down", "detail": "Upstream outage"})
        check("manual status set", r.status_code == 200 and r.json()["data"]["source"] == "manual", r.text)

        r = client.get("/status")
        j = r.json()
        by_key = {s["service"]: s for s in j["data"]}
        check("reported system shows operational", by_key["tts"]["status"] == "operational", str(by_key["tts"]))
        check("manual outage shows down with detail", by_key["chat"]["status"] == "down" and by_key["chat"]["detail"] == "Upstream outage", str(by_key["chat"]))
        check("silent systems stay unknown", by_key["ocr"]["status"] == "unknown", str(by_key["ocr"]))
        check("overall takes the worst status", j["overall"] == "down" and "outage" in j["message"].lower(), r.text)
        check("today's history bucket is filled", by_key["tts"]["history"][-1]["status"] == "operational", str(by_key["tts"]["history"][-3:]))

        # A recovered service must not erase the day's outage from the strip.
        client.post("/status/heartbeat", headers=secret, json={"service": "chat"})
        r = client.get("/status/chat")
        check("day rollup keeps the worst status seen", r.json()["data"]["history"][-1]["status"] == "down", str(r.json()["data"]["history"][-1]))
        check("but current status recovers", r.json()["data"]["status"] == "operational", r.text)
        r = client.get("/status/nope")
        check("unknown system -> 404", r.status_code == 404, r.text)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
