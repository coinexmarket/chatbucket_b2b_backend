"""Projects — a customer's own grouping for keys and the usage they generate.

    POST   /projects        create
    GET    /projects        list
    GET    /projects/{id}   one, with its key count
    PATCH  /projects/{id}   rename / re-describe
    DELETE /projects/{id}   delete, detaching its keys

Backs the "Select Project" field on the Create API Key modal. A project is
attached to an **API key**, and usage inherits the project of the key that
reported it — so the metering services never have to know about projects, and
attribution cannot drift from whichever key was actually used.

Deleting a project detaches its keys but **leaves historical usage alone**.
Rewriting past usage would change what a period cost under that project, which
is the one thing the attribution exists to record; the breakdown labels the
orphans instead.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.errors import DuplicateKeyError

from ..database import api_keys_collection, projects_collection
from ..deps import get_current_user
from ..models.projects import ProjectRequest, ProjectUpdateRequest, normalize_name
from ..serialization import iso

router = APIRouter(prefix="/projects", tags=["projects"])

_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Project not found."
)
_DUPLICATE = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="A project with this name already exists.",
)


def _oid(project_id: str) -> ObjectId:
    try:
        return ObjectId(project_id)
    except (InvalidId, TypeError):
        raise _NOT_FOUND


def _view(doc: dict, key_count: int | None = None) -> dict:
    created = doc.get("created_at")
    view = {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "description": doc.get("description"),
        "created_at": iso(created),
    }
    if key_count is not None:
        view["api_key_count"] = key_count
    return view


async def resolve_project(user: dict, project_id: str | None) -> str | None:
    """Validate that a project id belongs to this customer.

    Shared with the API-key routes: without this check a customer could attach
    their key to someone else's project by guessing an id.
    """
    if not project_id:
        return None
    doc = await projects_collection().find_one(
        {"_id": _oid(project_id), "user_id": user["_id"]}
    )
    if doc is None:
        raise _NOT_FOUND
    return str(doc["_id"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectRequest, user: dict = Depends(get_current_user)
):
    now = datetime.now(timezone.utc)
    document = {
        "user_id": user["_id"],
        "name": payload.name.strip(),
        # Case-folded key backing the unique index, so "Production" and
        # "production" cannot both exist and confuse the picker.
        "name_key": normalize_name(payload.name),
        "description": (payload.description or "").strip() or None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await projects_collection().insert_one(document)
    except DuplicateKeyError:
        raise _DUPLICATE
    document["_id"] = result.inserted_id
    return {"status": True, "data": _view(document, key_count=0)}


@router.get("")
async def list_projects(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    query = {"user_id": user["_id"]}
    total = await projects_collection().count_documents(query)
    docs = await (
        projects_collection().find(query).sort("created_at", -1).skip(offset).limit(limit)
    ).to_list(length=limit)

    # One grouped count rather than a query per project, so a customer with
    # fifty projects still costs two round trips.
    counts = await api_keys_collection().aggregate([
        {"$match": {"user_id": user["_id"], "project_id": {"$ne": None}}},
        {"$group": {"_id": "$project_id", "n": {"$sum": 1}}},
    ]).to_list(length=None)
    by_project = {row["_id"]: row["n"] for row in counts}

    return {
        "status": True,
        "count": len(docs),
        "total": total,
        "data": [_view(d, by_project.get(str(d["_id"]), 0)) for d in docs],
    }


@router.get("/{project_id}")
async def get_project(project_id: str, user: dict = Depends(get_current_user)):
    doc = await projects_collection().find_one(
        {"_id": _oid(project_id), "user_id": user["_id"]}
    )
    if doc is None:
        raise _NOT_FOUND
    keys = await api_keys_collection().count_documents(
        {"user_id": user["_id"], "project_id": str(doc["_id"])}
    )
    return {"status": True, "data": _view(doc, keys)}


@router.patch("/{project_id}")
async def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    user: dict = Depends(get_current_user),
):
    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if payload.name is not None:
        updates["name"] = payload.name.strip()
        updates["name_key"] = normalize_name(payload.name)
    if payload.description is not None:
        updates["description"] = payload.description.strip() or None
    if len(updates) == 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update."
        )

    try:
        doc = await projects_collection().find_one_and_update(
            {"_id": _oid(project_id), "user_id": user["_id"]},
            {"$set": updates},
            return_document=True,
        )
    except DuplicateKeyError:
        raise _DUPLICATE
    if doc is None:
        raise _NOT_FOUND
    return {"status": True, "data": _view(doc)}


@router.delete("/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(get_current_user)):
    """Delete a project and detach its keys.

    The keys keep working — a project is a label, not a credential, so removing
    it must not silently break a customer's integration. Historical usage keeps
    the project id it was recorded under.
    """
    oid = _oid(project_id)
    doc = await projects_collection().find_one({"_id": oid, "user_id": user["_id"]})
    if doc is None:
        raise _NOT_FOUND

    detached = await api_keys_collection().update_many(
        {"user_id": user["_id"], "project_id": str(oid)},
        {"$set": {"project_id": None}},
    )
    await projects_collection().delete_one({"_id": oid, "user_id": user["_id"]})
    return {
        "status": True,
        "message": "Project deleted. Its API keys still work, now unassigned.",
        "keys_detached": detached.modified_count,
    }
