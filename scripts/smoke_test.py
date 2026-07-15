"""End-to-end smoke test driving every endpoint through the real FastAPI app.

Uses an in-memory async Mongo (mongomock-motor) wired into the database layer,
so no real MongoDB is required. Run:  python -m scripts.smoke_test
Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app import database
from app.main import app


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wire_in_memory_db() -> None:
    """Point the database layer at an in-memory Mongo and seed data."""
    client = AsyncMongoMockClient()
    database._mongo.client = client  # type: ignore[attr-defined]
    database._mongo.blog_db = client["chatbucket"]  # type: ignore[attr-defined]
    database._mongo.contest_db = client["ChatBucketHackathon"]  # type: ignore


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

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
