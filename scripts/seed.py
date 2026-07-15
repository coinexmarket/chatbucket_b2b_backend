"""Seed the blog database with a few sample documents for local testing.

Usage:
    python -m scripts.seed

Reads MONGODB_URI / BLOG_DB_NAME from the environment (or .env) exactly like
the app. Safe to re-run: it upserts by slug/name.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


CATEGORIES = [
    {"name": "Product", "createdAt": _now()},
    {"name": "Engineering", "createdAt": _now()},
    {"name": "Community", "createdAt": _now()},
]

BLOGS = [
    {
        "title": "Introducing ChatBucket",
        "body": "<p>The full story of why we built ChatBucket.</p>",
        "slug": "introducing-chatbucket",
        "author": "ChatBucket Team",
        "meta_title": "Introducing ChatBucket",
        "meta_desc": "Meet ChatBucket — spatial, secure conversations.",
        "meta_keywords": "chatbucket, chat, spatial",
        "category": "Product",
        "sub_category": "Announcements",
        "tags": ["launch", "product"],
        "featured_img": "https://picsum.photos/seed/cb1/800/450",
        "og_image": "https://picsum.photos/seed/cb1/1200/630",
        "og_title": "Introducing ChatBucket",
        "og_description": "Meet ChatBucket.",
        "featured": True,
        "faq": [{"question": "Is it free?", "answer": "Yes, to start."}],
        "createdAt": _now(),
        "updatedAt": _now(),
    },
    {
        "title": "How we scaled real-time chat",
        "body": "<p>An engineering deep dive.</p>",
        "slug": "scaling-realtime-chat",
        "author": "Eng Team",
        "meta_title": "Scaling real-time chat",
        "meta_desc": "The architecture behind our real-time layer.",
        "meta_keywords": "scaling, websockets",
        "category": "Engineering",
        "tags": ["backend", "scaling"],
        "featured_img": "https://picsum.photos/seed/cb2/800/450",
        "og_image": "https://picsum.photos/seed/cb2/1200/630",
        "og_title": "Scaling real-time chat",
        "og_description": "Architecture deep dive.",
        "featured": False,
        "faq": [],
        "createdAt": _now(),
        "updatedAt": _now(),
    },
]


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True)
    db = client[settings.blog_db_name]

    for cat in CATEGORIES:
        await db["categories"].update_one(
            {"name": cat["name"]}, {"$setOnInsert": cat}, upsert=True
        )
    for blog in BLOGS:
        await db["blogs"].update_one(
            {"slug": blog["slug"]}, {"$set": blog}, upsert=True
        )

    n_blogs = await db["blogs"].count_documents({})
    n_cats = await db["categories"].count_documents({})
    print(f"Seeded. blogs={n_blogs} categories={n_cats} db={settings.blog_db_name}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
