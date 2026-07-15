"""Blog + category read endpoints.

These reproduce the contract the Next.js frontend calls at
``https://blogapi.chatbucket.chat``:

    GET /v1/blogs                         -> all blogs (used by sitemap)
    GET /v1/blogs/{slug}                  -> one full blog
    GET /v2/blogs/{slug}?category&sub_..  -> one full blog, category-scoped
    GET /v1/recent-blogs                  -> latest full blogs
    GET /v1/related-blogs/{category}      -> overviews in a category
    GET /v1/featured-blogs                -> featured overviews
    GET /v1/categories                    -> all categories
    GET /v1/c-blogs?categories&text       -> overviews filtered by cat + search
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..database import blogs_collection, categories_collection
from ..responses import failure, success
from ..serialization import serialize_doc, serialize_docs, to_overviews

router = APIRouter(tags=["blogs"])

RECENT_LIMIT = 6
FEATURED_LIMIT = 10
RELATED_LIMIT = 6


@router.get("/v1/blogs")
async def list_blogs():
    """All blogs (overview shape). The sitemap reads ``body.blogs``."""
    cursor = blogs_collection().find({}).sort("createdAt", -1)
    docs = await cursor.to_list(length=None)
    return success(to_overviews(docs), data_key="blogs")


@router.get("/v1/recent-blogs")
async def recent_blogs():
    """Most recent blogs (full shape — LatestBlogsSection renders IBlog[])."""
    cursor = blogs_collection().find({}).sort("createdAt", -1).limit(RECENT_LIMIT)
    docs = await cursor.to_list(length=RECENT_LIMIT)
    return success(serialize_docs(docs))


@router.get("/v1/featured-blogs")
async def featured_blogs():
    """Blogs flagged ``featured: true`` (overview shape)."""
    cursor = (
        blogs_collection()
        .find({"featured": True})
        .sort("createdAt", -1)
        .limit(FEATURED_LIMIT)
    )
    docs = await cursor.to_list(length=FEATURED_LIMIT)
    return success(to_overviews(docs))


@router.get("/v1/related-blogs/{category}")
async def related_blogs(category: str):
    """Other blogs in the same category (overview shape)."""
    cursor = (
        blogs_collection()
        .find({"category": category})
        .sort("createdAt", -1)
        .limit(RELATED_LIMIT)
    )
    docs = await cursor.to_list(length=RELATED_LIMIT)
    return success(to_overviews(docs))


@router.get("/v1/categories")
async def list_categories():
    """All categories. The frontend reads ``body.categories`` (dataKey)."""
    cursor = categories_collection().find({}).sort("name", 1)
    docs = await cursor.to_list(length=None)
    return success(serialize_docs(docs), data_key="categories")


@router.get("/v1/c-blogs")
async def category_blogs(
    categories: str | None = Query(default=None),
    text: str | None = Query(default=None),
):
    """Blogs filtered by selected categories and/or a free-text search.

    ``categories`` is a comma-separated list; ``text`` matches title/meta_desc.
    The frontend reads ``body.blogs`` (dataKey).
    """
    query: dict = {}
    if categories:
        wanted = [c.strip() for c in categories.split(",") if c.strip()]
        if wanted:
            query["category"] = {"$in": wanted}
    if text and text.strip():
        pattern = text.strip()
        query["$or"] = [
            {"title": {"$regex": pattern, "$options": "i"}},
            {"meta_desc": {"$regex": pattern, "$options": "i"}},
            {"tags": {"$regex": pattern, "$options": "i"}},
        ]

    cursor = blogs_collection().find(query).sort("createdAt", -1)
    docs = await cursor.to_list(length=None)
    return success(to_overviews(docs), data_key="blogs")


@router.get("/v1/blogs/{slug}")
async def get_blog(slug: str):
    """A single full blog by slug."""
    doc = await blogs_collection().find_one({"slug": slug})
    if not doc:
        return failure("Blog not found", status_code=404)
    return success(serialize_doc(doc))


@router.get("/v2/blogs/{slug}")
async def get_blog_v2(
    slug: str,
    category: str | None = Query(default=None),
    sub_category: str | None = Query(default=None),
):
    """A single full blog by slug, optionally scoped to a category path."""
    query: dict = {"slug": slug}
    if category:
        query["category"] = category
    if sub_category:
        query["sub_category"] = sub_category

    doc = await blogs_collection().find_one(query)
    if not doc:
        return failure("Blog not found", status_code=404)
    return success(serialize_doc(doc))
