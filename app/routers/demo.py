"""Demo requests — the "Let's get your demo started" modal.

    POST /demo-requests   { "type": "personal" | "business", ... }

One endpoint for both tabs of the modal. The body is a discriminated union on
``type`` (see ``models/requests.py``), so the Business tab's ``company_name``
is enforced without making it required for Personal leads.

Public and unauthenticated, because it is a marketing form. Duplicates are
accepted on purpose: someone asking for a second demo months later is a lead,
not a mistake, so de-duplication belongs in whatever CRM consumes these rather
than in a 409 that loses the request.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, status

from .. import ratelimit
from ..database import demo_requests_collection
from ..email import send_contact_received, send_demo_request_notification
from ..models.requests import DemoRequestBody

router = APIRouter(prefix="/demo-requests", tags=["demo"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    # Public and unauthenticated, so it is a spam target — and it sends mail.
    dependencies=[Depends(ratelimit.by_ip("demo_ip"))],
)
async def create_demo_request(
    payload: DemoRequestBody, background_tasks: BackgroundTasks
):
    """Record a demo request from the Personal or Business tab."""
    document = payload.model_dump()

    # Normalise the shared identity fields the same way the auth endpoints do,
    # so a lead typed as " Ada@X.com " matches one typed as "ada@x.com".
    document["email"] = document["email"].lower().strip()
    document["name"] = document["name"].strip()
    for optional in ("mobile", "company_name", "company_details", "how_did_you_hear"):
        value = document.get(optional)
        if isinstance(value, str):
            document[optional] = value.strip() or None

    document["status"] = "new"  # for whoever works the lead queue
    document["created_at"] = datetime.now(timezone.utc)

    result = await demo_requests_collection().insert_one(document)

    # Notify sales *and* acknowledge to the person who wrote in, both after the
    # response goes out: the lead is already safely stored, so a mail outage
    # must not fail the submission and lose it.
    lead = {**document, "_id": str(result.inserted_id)}
    background_tasks.add_task(send_demo_request_notification, lead)
    # The lead id doubles as the query id the acknowledgement tells them to
    # quote — one identifier for the customer, sales and support to share.
    background_tasks.add_task(send_contact_received, lead)

    return {
        "status": True,
        "message": "Thanks — we'll be in touch to schedule your demo.",
        "data": {
            "id": str(result.inserted_id),
            "type": document["type"],
            "created_at": document["created_at"].isoformat(),
        },
    }
