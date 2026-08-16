"""End-to-end smoke test for the B2B platform endpoints.

Drives the real FastAPI app against an in-memory async Mongo, exercising the
whole customer journey: register -> login -> profile -> API key -> meter usage
-> history/summary -> pricing -> password reset.

Run:  python -m scripts.smoke_b2b   (exits non-zero on any failure)
"""
from __future__ import annotations

import os

# Ignore any local `.env`: the suite must assert the same thing on every
# machine. A developer's `SIGNUP_BONUS_CREDITS=1000` would otherwise start each
# account with credits the balance assertions below expect it not to have.
os.environ["CHATBUCKET_IGNORE_DOTENV"] = "1"

# Everything the assertions depend on is pinned here rather than left to the
# defaults in config.py, so that changing a default fails loudly on the line
# that owns it instead of somewhere in the middle of the billing block. Set
# before `app` is imported, because settings are built (and cached) at import.
os.environ["EMAIL_BACKEND"] = "memory"  # capture mail instead of sending it
os.environ["APP_BASE_URL"] = "https://app.example.com"
os.environ["BILLING_WEBHOOK_SECRET"] = "test-webhook-secret"
# Off by default so the suite can register and log in freely; the dedicated
# rate-limit block below switches it on and asserts the 429s.
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["ENVIRONMENT"] = "development"  # forgot-password returns the token
os.environ["SIGNUP_BONUS_CREDITS"] = "0"  # accounts start empty and top up
os.environ["ENFORCE_CREDIT_BALANCE"] = "true"  # the 402-on-exhausted path
os.environ["ENFORCE_PLAN_RATE_LIMITS"] = "true"  # metering runs under the cap
os.environ["REQUIRE_EMAIL_VERIFICATION"] = "false"  # ditto
os.environ["TERMS_VERSION"] = "v1"  # asserted on the signup response

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import anyio  # noqa: E402
from bson import ObjectId  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import credits, database, payments, pricing, ratelimit  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.email import _build_message, outbox  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def freeze_limiter_clock():
    """Stop the limiter's clock. Returns the real module, to restore after.

    `ratelimit.hit` derives its fixed window from wall-clock time
    (`now - now % window_seconds`), so a burst that straddles a boundary is
    counted against two windows and never reaches the cap. These assertions
    therefore failed whenever a run happened to cross a minute mark mid-burst —
    a fact about *when* the suite ran, not about the code under test, and one
    that blocked deploys at random since a red suite stops the pipeline.

    Holding the clock still does not weaken what is asserted. Every request in
    the burst lands in one window, which is precisely the case the limit exists
    to handle; the boundary behaviour it used to depend on was never the thing
    being tested.
    """
    real = ratelimit.time
    now = real.time()
    ratelimit.time = SimpleNamespace(time=lambda: now)
    return real


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def wire_in_memory_db() -> None:
    client = AsyncMongoMockClient()
    database._mongo.client = client  # type: ignore[attr-defined]
    database._mongo.b2b_db = client["chatbucket_b2b"]  # type: ignore
    database._mongo.blog_db = client["chatbucket"]  # type: ignore
    database._mongo.contest_db = client["ChatBucketHackathon"]  # type: ignore


def main() -> int:
    wire_in_memory_db()

    with TestClient(app) as client:
        # --- pricing (public) ------------------------------------------
        r = client.get("/pricing")
        j = r.json()
        check(
            "GET /pricing lists all 8 services",
            r.status_code == 200 and len(j["data"]) == 8,
            r.text,
        )
        check(
            "pricing includes voip_call @ 5/min",
            any(x["service"] == "voip_call" and x["rate"] == 5.0 for x in j["data"]),
        )

        # --- estimate (public) -----------------------------------------
        r = client.post("/usage/estimate", json={"service": "stt_streaming", "quantity": 10})
        check("estimate stt_streaming 10min = 5.2", r.json()["data"]["cost"] == 5.2, r.text)
        r = client.post("/usage/estimate", json={"service": "tts_streaming", "quantity": 2500})
        check("estimate tts_streaming 2500ch = 2.275", r.json()["data"]["cost"] == 2.275, r.text)
        r = client.post("/usage/estimate", json={"service": "translation", "quantity": 5000})
        check("estimate translation 5k tok = 3.75", r.json()["data"]["cost"] == 3.75, r.text)
        r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000})
        check("estimate chat_agent 10k tok = 4.38", r.json()["data"]["cost"] == 4.38, r.text)
        r = client.post("/usage/estimate", json={"service": "voip_call", "quantity": 3})
        check("estimate voip_call 3min = 15.0", r.json()["data"]["cost"] == 15.0, r.text)
        r = client.post("/usage/estimate", json={"service": "nope", "quantity": 1})
        check("estimate unknown service -> 400", r.status_code == 400, r.text)

        # --- register --------------------------------------------------
        r = client.post(
            "/auth/register",
            json={
                "name": "Acme Corp",
                "email": "ops@acme.io",
                "password": "supersecret1",
                "company": "Acme",
                # As the signup form sends it: dial code included, user-typed
                # spacing, camelCase keys.
                "mobile": "+91 98765-43210",
                "howDidYouHear": "Google search",
                "acceptTerms": True,
            },
        )
        j = r.json()
        token = j.get("access_token", "")
        check("POST /auth/register 201 + token", r.status_code == 201 and bool(token), r.text)
        check("register response hides password_hash", "password_hash" not in j["user"], r.text)
        check("mobile normalised to E.164", j["user"].get("phone") == "+919876543210", r.text)
        check("register stores how-did-you-hear", j["user"].get("how_did_you_hear") == "Google search", r.text)
        check("terms acceptance is dated + versioned", bool(j["user"].get("terms_accepted_at")) and j["user"].get("terms_version") == "v1", r.text)
        check("new account starts unverified", j["user"].get("email_verified") is False, r.text)
        verify_token = j.get("verification_token")
        verify_code = j.get("verification_code")
        check("register returns a dev verification token", bool(verify_token), r.text)
        check("register returns a dev verification code", bool(verify_code) and len(verify_code) == 6, r.text)
        verification_mail = next(
            (m for m in outbox if m["subject"] == f"{verify_code} is your ChatBucket verification code"),
            None,
        )
        check("verification email sent", verification_mail is not None, str([m["subject"] for m in outbox]))
        check("verification email carries the link", f"?token={verify_token}" in (verification_mail or {}).get("body", ""), str(verification_mail))
        check("verification email carries the code", verify_code in (verification_mail or {}).get("body", ""), str(verification_mail))
        # Each digit is set in its own box, so the code only appears in the
        # HTML split apart — assert the boxes, not the joined string.
        verification_html = (verification_mail or {}).get("html") or ""
        check("verification email has an HTML part", verification_html.startswith("<!DOCTYPE html>"), verification_html[:80])
        check(
            "OTP digits rendered into the design's boxes",
            all(
                f'text-align:center;">{digit}</div>' in verification_html
                for digit in verify_code or "x"
            ),
            verification_html[:80],
        )
        check("HTML carries no unrendered placeholders", "{{" not in verification_html, verification_html[:80])

        welcome_mail = next((m for m in outbox if m["subject"] == "Welcome to ChatBucket"), None)
        check("welcome email sent on register", welcome_mail is not None, str([m["subject"] for m in outbox]))
        check("welcome email is addressed to the new account", (welcome_mail or {}).get("to") == "ops@acme.io", str(welcome_mail))
        check(
            "welcome email hides the credit panel when no bonus is granted",
            "Free Credits" not in ((welcome_mail or {}).get("html") or ""),
            str((welcome_mail or {}).get("html", ""))[:200],
        )

        def register_body(**overrides):
            body = {
                "name": "Dup",
                "email": "ops@acme.io",
                "password": "supersecret1",
                "mobile": "+919876500000",
                "accept_terms": True,
            }
            body.update(overrides)
            return body

        r = client.post("/auth/register", json=register_body())
        check("register duplicate email -> 409", r.status_code == 409, r.text)

        # --- signup form validation -------------------------------------
        r = client.post("/auth/register", json=register_body(email="t1@x.io", accept_terms=False))
        check("terms not accepted -> 422", r.status_code == 422, r.text)
        body = register_body(email="t2@x.io")
        del body["accept_terms"]
        r = client.post("/auth/register", json=body)
        check("terms field omitted -> 422", r.status_code == 422, r.text)
        body = register_body(email="t3@x.io")
        del body["mobile"]
        r = client.post("/auth/register", json=body)
        check("mobile omitted -> 422", r.status_code == 422, r.text)
        r = client.post("/auth/register", json=register_body(email="t4@x.io", mobile="9876543210"))
        check("mobile without country code -> 422", r.status_code == 422, r.text)
        r = client.post("/auth/register", json=register_body(email="t5@x.io", mobile="+91987"))
        check("mobile too short -> 422", r.status_code == 422, r.text)
        r = client.post("/auth/register", json=register_body(email="t6@x.io", mobile="+0919876543210"))
        check("mobile with 0 country code -> 422", r.status_code == 422, r.text)
        # The whole point of extra="forbid": a field the API doesn't model must
        # fail loudly instead of being silently dropped from a 201.
        r = client.post("/auth/register", json=register_body(email="t7@x.io", newsletter=True))
        check("unknown signup field -> 422, not silently dropped", r.status_code == 422, r.text)

        # --- login -----------------------------------------------------
        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "supersecret1"})
        check("POST /auth/login 200 + token", r.status_code == 200 and r.json().get("access_token"), r.text)
        token = r.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "wrong"})
        check("login wrong password -> 401", r.status_code == 401, r.text)

        # --- profile requires auth -------------------------------------
        r = client.get("/profile")
        check("GET /profile no token -> 401", r.status_code == 401, r.text)
        r = client.get("/profile", headers=auth)
        check("GET /profile with token", r.status_code == 200 and r.json()["data"]["email"] == "ops@acme.io", r.text)

        r = client.put("/profile", headers=auth, json={"company": "Acme Global", "phone": "+91 99999 88888"})
        check("PUT /profile updates company", r.json()["data"]["company"] == "Acme Global", r.text)
        check("PUT /profile normalises phone too", r.json()["data"]["phone"] == "+919999988888", r.text)
        r = client.put("/profile", headers=auth, json={"phone": "99999"})
        check("PUT /profile rejects non-E.164 phone -> 422", r.status_code == 422, r.text)

        # --- change password + re-login --------------------------------
        r = client.put("/profile/password", headers=auth, json={"current_password": "supersecret1", "new_password": "newsecret22"})
        check("PUT /profile/password ok", r.status_code == 200, r.text)
        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "newsecret22"})
        check("login with new password", r.status_code == 200, r.text)
        token = r.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # --- API key create/list/use ----------------------------------
        r = client.post("/api-keys", headers=auth, json={"name": "prod"})
        j = r.json()
        api_key = j.get("api_key", "")
        key_id = j["data"]["id"]
        check("POST /api-keys returns plaintext once", r.status_code == 201 and api_key.startswith("cb_live_"), r.text)
        r = client.get("/api-keys", headers=auth)
        check("GET /api-keys masks key", r.json()["data"][0]["masked_key"].startswith("cb_live_****"), r.text)
        check("GET /api-keys reports paging totals", r.json()["total"] == 1 and r.json()["limit"] == 50 and r.json()["offset"] == 0, r.text)

        # --- rename ------------------------------------------------------
        key_id = r.json()["data"][0]["id"]
        r = client.patch(f"/api-keys/{key_id}", headers=auth, json={"name": "Production"})
        check("PATCH /api-keys renames", r.status_code == 200 and r.json()["data"]["name"] == "Production", r.text)
        r = client.get("/api-keys", headers=auth)
        check("rename persists", r.json()["data"][0]["name"] == "Production", r.text)
        check("rename leaves the secret alone", r.json()["data"][0]["masked_key"].startswith("cb_live_****"), r.text)
        r = client.patch(f"/api-keys/{key_id}", headers=auth, json={"name": ""})
        check("rename to empty -> 422", r.status_code == 422, r.text)
        r = client.patch(f"/api-keys/{key_id}", json={"name": "x"})
        check("rename without token -> 401", r.status_code == 401, r.text)
        r = client.patch("/api-keys/not-an-id", headers=auth, json={"name": "x"})
        check("rename bad id -> 404", r.status_code == 404, r.text)
        r = client.patch(f"/api-keys/{ObjectId()}", headers=auth, json={"name": "x"})
        check("rename unknown key -> 404", r.status_code == 404, r.text)

        # --- key verification (what the AI services call) --------------------
        # Our STT/TTS/chat services need to know *which* customer is calling
        # before doing the work, so the usage can be billed to them.
        r = client.post("/api-keys/verify", headers={"X-API-Key": api_key})
        j = r.json().get("data", {})
        check("POST /api-keys/verify accepts a live key", r.status_code == 200, r.text)
        check("verify names the owning account", j.get("api_key_id") == key_id, r.text)
        check("verify reports the plan's rate limit", j.get("requests_per_minute") == 60, r.text)
        check("verify reports the credit balance", "credits" in j and "has_credits" in j, r.text)
        check("verify is never cached", r.headers.get("Cache-Control") == "no-store", str(r.headers))
        r = client.post("/api-keys/verify", headers={"X-API-Key": "cb_live_bogus"})
        check("verify rejects an unknown key -> 401", r.status_code == 401, r.text)
        r = client.post("/api-keys/verify")
        check("verify without a key -> 401", r.status_code == 401, r.text)

        # Cross-tenant: another customer's key must be untouchable even with a
        # valid id, for rename and revoke alike.
        r = client.post("/auth/register", json={
            "name": "Rival", "email": "rival@other.io", "password": "supersecret1",
            "mobile": "+919000000001", "acceptTerms": True})
        rival_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = client.post("/api-keys", headers=rival_auth, json={"name": "Rival key"})
        rival_key_id = r.json()["data"]["id"]
        r = client.patch(f"/api-keys/{rival_key_id}", headers=auth, json={"name": "stolen"})
        check("cannot rename another customer's key -> 404", r.status_code == 404, r.text)
        r = client.delete(f"/api-keys/{rival_key_id}", headers=auth)
        check("cannot revoke another customer's key -> 404", r.status_code == 404, r.text)
        r = client.get("/api-keys", headers=rival_auth)
        check("rival's key is untouched", r.json()["data"][0]["name"] == "Rival key" and r.json()["data"][0]["revoked"] is False, r.text)

        # Paging, exercised on the rival account so the key ordering the main
        # account's later revoke tests depend on is left alone.
        client.post("/api-keys", headers=rival_auth, json={"name": "Rival second"})
        r = client.get("/api-keys?limit=1", headers=rival_auth)
        check("GET /api-keys pages", r.json()["count"] == 1 and r.json()["total"] == 2, r.text)
        r = client.get("/api-keys?limit=1&offset=1", headers=rival_auth)
        check("offset walks the pages", r.json()["count"] == 1 and r.json()["data"][0]["name"] == "Rival key", r.text)
        client.delete(f"/api-keys/{rival_key_id}", headers=rival_auth)
        r = client.get("/api-keys?include_revoked=false", headers=rival_auth)
        check("include_revoked=false hides revoked keys", r.json()["total"] == 1, r.text)
        r = client.get("/api-keys", headers=rival_auth)
        check("revoked keys still listed by default", r.json()["total"] == 2, r.text)

        # --- limits ------------------------------------------------------
        r = client.get("/limits/plans")
        plans = r.json()["data"]
        check("GET /limits/plans lists 3 tiers", r.status_code == 200 and len(plans) == 3, r.text)
        pro = next(p for p in plans if p["plan"] == "pro")
        check("pro pack is 10000 -> 11000 credits", pro["price"] == 10000.0 and pro["credits"] == 11000.0 and pro["bonus_credits"] == 1000.0, str(pro))
        check("starter is not purchasable", next(p for p in plans if p["plan"] == "starter")["purchasable"] is False, str(plans))

        r = client.get("/limits", headers=auth)
        j = r.json()["data"]
        check("GET /limits defaults to starter @60rpm", r.status_code == 200 and j["plan"] == "starter" and j["requests_per_minute"] == 60, r.text)
        check("GET /limits lists every service", len(j["limits"]) == 8, r.text)
        check("GET /limits reports limits are enforced", j["enforced"] is True, r.text)
        r = client.get("/limits")
        check("GET /limits needs auth -> 401", r.status_code == 401, r.text)

        # --- billing: empty account ---------------------------------------
        r = client.get("/billing", headers=auth)
        j = r.json()["data"]
        check("GET /billing starts at 0 credits", r.status_code == 200 and j["credits"] == 0.0, r.text)
        check("auto-recharge starts disabled", j["auto_recharge"]["enabled"] is False, r.text)

        # --- metered call with no credits ---------------------------------
        headers_key = {"X-API-Key": api_key}
        r = client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 1})
        check("POST /usage with 0 credits -> 402", r.status_code == 402, r.text)
        r = client.get("/usage", headers=auth)
        unbilled = r.json()["data"]
        check("unpaid usage is still recorded", len(unbilled) == 1 and unbilled[0]["billed"] is False, r.text)

        # --- top up --------------------------------------------------------
        r = client.post("/billing/top-up", headers=auth, json={"amountInr": 500})
        j = r.json()["data"]
        payment_id = j["id"]
        check("POST /billing/top-up creates pending payment", r.status_code == 201 and j["status"] == "pending" and j["credits"] == 500.0, r.text)
        r = client.get("/billing", headers=auth)
        check("pending top-up grants no credits yet", r.json()["data"]["credits"] == 0.0, r.text)

        r = client.post("/billing/top-up", headers=auth, json={"amountInr": 500, "plan": "pro"})
        check("top-up with both plan and amount -> 422", r.status_code == 422, r.text)
        r = client.post("/billing/top-up", headers=auth, json={"plan": "starter"})
        check("top-up with unpurchasable plan -> 400", r.status_code == 400, r.text)

        # --- confirm (gateway webhook) --------------------------------------
        r = client.post(f"/billing/payments/{payment_id}/confirm", json={"providerPaymentId": "pay_1"})
        check("confirm without secret -> 401", r.status_code == 401, r.text)
        r = client.post(f"/billing/payments/{payment_id}/confirm", headers={"X-Billing-Secret": "wrong"}, json={"providerPaymentId": "pay_1"})
        check("confirm with wrong secret -> 401", r.status_code == 401, r.text)

        secret_hdr = {"X-Billing-Secret": "test-webhook-secret"}
        r = client.post(f"/billing/payments/{payment_id}/confirm", headers=secret_hdr, json={"providerPaymentId": "pay_1", "method": "UPI"})
        check("confirm grants credits", r.status_code == 200 and r.json()["replayed"] is False, r.text)
        r = client.get("/billing", headers=auth)
        check("balance is now 500", r.json()["data"]["credits"] == 500.0, r.text)

        # Gateways redeliver webhooks; a replay must not credit twice.
        r = client.post(f"/billing/payments/{payment_id}/confirm", headers=secret_hdr, json={"providerPaymentId": "pay_1"})
        check("confirm replay -> replayed, no double credit", r.status_code == 200 and r.json()["replayed"] is True, r.text)
        r = client.get("/billing", headers=auth)
        check("balance still 500 after replay", r.json()["data"]["credits"] == 500.0, r.text)

        # --- record usage (needs API key, not JWT) ---------------------
        r = client.post("/usage", json={"service": "stt_streaming", "quantity": 10})
        check("POST /usage without API key -> 401", r.status_code == 401, r.text)

        r = client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 10})
        check("POST /usage stt 10min cost 5.2", r.status_code == 201 and r.json()["data"]["cost"] == 5.2, r.text)
        check("usage debits credits (500 - 5.2)", r.json().get("balance") == 494.8, r.text)
        check("paid usage is marked billed", r.json()["data"]["billed"] is True, r.text)
        client.post("/usage", headers=headers_key, json={"service": "tts_offline", "quantity": 2000})  # 0.78*2 = 1.56
        client.post("/usage", headers=headers_key, json={"service": "voip_call", "quantity": 4})       # 20.0
        client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 5})   # 2.6

        r = client.post("/usage", headers={"X-API-Key": "cb_live_bogus"}, json={"service": "stt_streaming", "quantity": 1})
        check("POST /usage bad API key -> 401", r.status_code == 401, r.text)

        # --- history + summary (JWT) -----------------------------------
        r = client.get("/usage", headers=auth)
        check("GET /usage history count = 5", r.json()["count"] == 5, r.text)
        r = client.get("/usage?service=stt_streaming", headers=auth)
        check("GET /usage filtered by service = 3", r.json()["count"] == 3, r.text)

        r = client.get("/usage/summary", headers=auth)
        j = r.json()
        # 5.2 + 1.56 + 20.0 + 2.6 = 29.36
        # 0.52 of this was consumed before the account had credits, so it is
        # counted as consumption but never as revenue.
        check("GET /usage/summary grand_total = 29.88", j["grand_total"] == 29.88, r.text)
        check("summary separates unbilled consumption", j["unbilled_total"] == 0.52 and j["billed_total"] == 29.36, r.text)
        stt = next((s for s in j["by_service"] if s["service"] == "stt_streaming"), None)
        check("summary stt_streaming total 8.32 over 3 events", stt and stt["total_cost"] == 8.32 and stt["events"] == 3, str(stt))

        # --- per-model tracking ---------------------------------------------
        # Same model, three spellings the callers might send.
        client.post("/usage", headers=headers_key, json={"service": "tts_streaming", "quantity": 1000, "model": "Bulbul v3"})     # 0.91
        client.post("/usage", headers=headers_key, json={"service": "tts_streaming", "quantity": 1000, "model": "bulbul  V3 "})   # 0.91
        client.post("/usage", headers=headers_key, json={"service": "translation", "quantity": 10000, "model": "Mayura v1 Formal"})  # 7.5

        r = client.get("/usage/summary", headers=auth)
        j = r.json()
        models = {row["model_key"]: row for row in j["by_model"]}
        check("summary breaks down by model", len(models) == 2, str(j["by_model"]))
        check("model spelling variants merge into one row", models["bulbul v3"]["events"] == 2 and models["bulbul v3"]["total_cost"] == 1.82, str(models.get("bulbul v3")))
        check("model row keeps the display spelling", models["bulbul v3"]["model"] == "Bulbul v3", str(models["bulbul v3"]))
        check("models ranked by spend", j["by_model"][0]["model_key"] == "mayura v1 formal", str(j["by_model"]))
        check("model share_percent is reported", 0 < models["mayura v1 formal"]["share_percent"] < 100, str(models["mayura v1 formal"]))
        # The five earlier calls reported no model, so they must show up as
        # unattributed rather than silently vanishing from the reconciliation.
        check("usage without a model is unattributed, not lost", j["unattributed_cost"] == 29.88, r.text)
        check("attributed + unattributed = grand_total", round(sum(m["total_cost"] for m in j["by_model"]) + j["unattributed_cost"], 4) == j["grand_total"], r.text)

        r = client.get("/usage?model=BULBUL   V3", headers=auth)
        check("?model= filter is case/space insensitive", r.json()["count"] == 2, r.text)
        r = client.get("/usage?model=nope", headers=auth)
        check("?model= unknown returns nothing", r.json()["count"] == 0, r.text)
        r = client.get("/usage?service=tts_streaming&model=Bulbul v3", headers=auth)
        check("service + model filters combine", r.json()["count"] == 2, r.text)

        r = client.post("/usage", headers=headers_key, json={"service": "stt_offline", "quantity": 1, "model": "   "})
        check("whitespace-only model stored as absent", r.status_code == 201 and r.json()["data"]["model"] is None, r.text)
        r = client.post("/usage", headers=headers_key, json={"service": "stt_offline", "quantity": 1, "model": "x" * 65})
        check("model over 64 chars -> 422", r.status_code == 422, r.text)
        r = client.post("/usage", headers=headers_key, json={"service": "stt_offline", "quantity": 1})
        check("model stays optional", r.status_code == 201 and r.json()["data"]["model"] is None, r.text)

        # --- timeseries -------------------------------------------------------
        r = client.get("/usage/timeseries", headers=auth)
        j = r.json()
        check("GET /usage/timeseries defaults to 30 daily buckets", r.status_code == 200 and j["granularity"] == "daily" and j["count"] == 31, r.text)
        check("timeseries totals match the usage recorded", j["totals"]["cost"] == 39.98 and j["totals"]["requests"] == 10, str(j["totals"]))
        check("empty buckets are zero-filled, not omitted", all(b["requests"] == 0 for b in j["data"][:-1]) and j["data"][-1]["requests"] == 10, str(j["data"][-3:]))
        check("bucket labels are ISO dates", len(j["data"][0]["bucket"]) == 10 and j["data"][0]["bucket"][4] == "-", str(j["data"][0]))

        r = client.get("/usage/timeseries?granularity=hourly", headers=auth)
        check("hourly granularity buckets by hour", r.json()["granularity"] == "hourly" and "T" in r.json()["data"][0]["bucket"], r.text)
        r = client.get("/usage/timeseries?granularity=minute", headers=auth)
        check("minute granularity works", r.json()["granularity"] == "minute" and r.json()["totals"]["requests"] == 10, r.text)
        r = client.get("/usage/timeseries?granularity=weekly", headers=auth)
        check("unknown granularity -> 400", r.status_code == 400, r.text)
        r = client.get("/usage/timeseries?granularity=minute&from=2020-01-01&to=2026-01-01", headers=auth)
        check("over-long minute range -> 400, not a huge payload", r.status_code == 400 and "at most" in r.json()["detail"], r.text)
        r = client.get("/usage/timeseries?from=2026-05-01&to=2026-04-01", headers=auth)
        check("from after to -> 400", r.status_code == 400, r.text)
        r = client.get("/usage/timeseries?from=2020-01-01&to=2020-01-05", headers=auth)
        check("range with no usage returns zeroed buckets", r.json()["count"] == 5 and r.json()["totals"]["requests"] == 0, r.text)
        r = client.get("/usage/timeseries?service=tts_streaming", headers=auth)
        check("timeseries honours the service filter", r.json()["totals"]["requests"] == 2, r.text)
        r = client.get("/usage/timeseries?model=Bulbul v3", headers=auth)
        check("timeseries honours the model filter", r.json()["totals"]["requests"] == 2, r.text)
        r = client.get("/usage/timeseries")
        check("timeseries needs auth -> 401", r.status_code == 401, r.text)

        # --- per-API-key attribution -------------------------------------------
        r = client.get("/usage/summary", headers=auth)
        keys_rows = r.json()["by_api_key"]
        check("summary breaks down by API key", len(keys_rows) == 1 and keys_rows[0]["events"] == 10, str(keys_rows))
        check("key breakdown carries the key's name", keys_rows[0]["name"] == "Production" and keys_rows[0]["masked_key"].startswith("cb_live_****"), str(keys_rows))
        this_key = keys_rows[0]["api_key_id"]
        r = client.get(f"/usage?api_key_id={this_key}", headers=auth)
        check("?api_key_id= filters history", r.json()["count"] == 10, r.text)
        r = client.get(f"/usage?api_key_id={ObjectId()}", headers=auth)
        check("unknown api_key_id returns nothing", r.json()["count"] == 0, r.text)
        r = client.get(f"/usage/timeseries?api_key_id={this_key}", headers=auth)
        check("timeseries honours the key filter", r.json()["totals"]["requests"] == 10, r.text)

        # --- overview -----------------------------------------------------------
        r = client.get("/usage/overview", headers=auth)
        j = r.json()
        check("GET /usage/overview reports the period", r.status_code == 200 and j["period"]["days"] == 30, r.text)
        check("overview current totals", j["current"]["cost"] == 39.98 and j["current"]["requests"] == 10, str(j["current"]))
        check("overview previous period is empty", j["previous"]["requests"] == 0, str(j["previous"]))
        check("no baseline -> null change, not a fake 0%", j["change_percent"]["cost"] is None, str(j["change_percent"]))
        # 39.98 consumed less the 0.52 that ran before any credits existed.
        check("overview carries plan + credit balance", j["plan"] == "starter" and j["credits"] == 460.54, r.text)
        r = client.get("/usage/overview?days=400", headers=auth)
        check("overview days over 365 -> 422", r.status_code == 422, r.text)
        r = client.get("/usage/overview")
        check("overview needs auth -> 401", r.status_code == 401, r.text)

        # --- projects ---------------------------------------------------------
        r = client.post("/projects", headers=auth, json={"name": "Mobile App", "description": "iOS + Android"})
        proj = r.json()["data"]
        check("POST /projects creates", r.status_code == 201 and proj["name"] == "Mobile App" and proj["api_key_count"] == 0, r.text)
        r = client.post("/projects", headers=auth, json={"name": "  mobile   app "})
        check("duplicate project name (case/space) -> 409", r.status_code == 409, r.text)
        r = client.post("/projects", headers=auth, json={"name": ""})
        check("empty project name -> 422", r.status_code == 422, r.text)
        r = client.post("/projects", headers=auth, json={"name": "Web"})
        web_id = r.json()["data"]["id"]
        r = client.get("/projects", headers=auth)
        check("GET /projects lists both", r.json()["total"] == 2, r.text)

        r = client.patch(f"/projects/{web_id}", headers=auth, json={"name": "Web App"})
        check("PATCH /projects renames", r.status_code == 200 and r.json()["data"]["name"] == "Web App", r.text)
        r = client.patch(f"/projects/{web_id}", headers=auth, json={})
        check("PATCH with no fields -> 400", r.status_code == 400, r.text)
        r = client.get(f"/projects/{ObjectId()}", headers=auth)
        check("unknown project -> 404", r.status_code == 404, r.text)
        r = client.get(f"/projects/{proj['id']}", headers=rival_auth)
        check("cannot read another customer's project -> 404", r.status_code == 404, r.text)
        r = client.post("/api-keys", headers=rival_auth, json={"name": "x", "projectId": proj["id"]})
        check("cannot attach a key to another customer's project -> 404", r.status_code == 404, r.text)

        # A key carries its project, and usage inherits it from the key.
        r = client.post("/api-keys", headers=auth, json={"name": "Mobile key", "projectId": proj["id"]})
        mobile_key = r.json()["api_key"]
        check("API key stores its project", r.json()["data"]["project_id"] == proj["id"], r.text)
        r = client.get(f"/projects/{proj['id']}", headers=auth)
        check("project reports its key count", r.json()["data"]["api_key_count"] == 1, r.text)

        client.post("/usage", headers={"X-API-Key": mobile_key}, json={"service": "translation", "quantity": 10000})
        r = client.get(f"/usage?project_id={proj['id']}", headers=auth)
        check("usage inherits the key's project", r.json()["count"] == 1 and r.json()["data"][0]["project_id"] == proj["id"], r.text)
        r = client.get(f"/usage/timeseries?project_id={proj['id']}", headers=auth)
        check("timeseries honours the project filter", r.json()["totals"]["requests"] == 1, r.text)

        # Deleting a project must not break the key that used it.
        r = client.delete(f"/projects/{proj['id']}", headers=auth)
        check("DELETE /projects detaches its keys", r.status_code == 200 and r.json()["keys_detached"] == 1, r.text)
        r = client.post("/usage", headers={"X-API-Key": mobile_key}, json={"service": "translation", "quantity": 1000})
        check("key still works after its project is deleted", r.status_code == 201, r.text)
        r = client.get(f"/usage?project_id={proj['id']}", headers=auth)
        check("historical usage keeps its project id", r.json()["count"] == 1, r.text)
        r = client.get("/usage/summary", headers=auth)
        rows = {x["project_id"]: x for x in r.json()["by_project"]}
        # One event, not two: the call made after the project was deleted went
        # through a detached key and so carries no project at all.
        check("summary breaks down by project", proj["id"] in rows and rows[proj["id"]]["events"] == 1, str(r.json()["by_project"]))
        check("usage of a deleted project is labelled, not blank", rows[proj["id"]]["name"] == "(deleted project)", str(rows[proj["id"]]))

        # --- CSV export ---------------------------------------------------------
        r = client.get("/usage/export.csv", headers=auth)
        body = r.text
        lines = [ln for ln in body.strip().split("\n") if ln]
        check("GET /usage/export.csv returns CSV", r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), str(r.headers))
        check("CSV offers a filename", "attachment; filename=" in r.headers.get("content-disposition", ""), str(r.headers))
        check("CSV header names the columns", lines[0].startswith("created_at,service,label,model,quantity"), lines[0])
        recorded = client.get("/usage?limit=500", headers=auth).json()["count"]
        check("CSV has a row per usage record", len(lines) - 1 == recorded, f"{len(lines)-1} csv rows vs {recorded} records")
        check("CSV carries exact decimals", any(",7.5," in ln for ln in lines[1:]), lines[1])
        r = client.get("/usage/export.csv?service=translation", headers=auth)
        check("CSV honours filters", len(r.text.strip().split("\n")) - 1 == 3, r.text[:200])
        r = client.get("/usage/export.csv?from=not-a-date", headers=auth)
        check("CSV bad date -> 400", r.status_code == 400, r.text)
        r = client.get("/usage/export.csv")
        check("CSV needs auth -> 401", r.status_code == 401, r.text)

        # --- per-model pricing ------------------------------------------------
        # MODEL_RATES ships empty (no invented prices), so register a couple of
        # overrides here to exercise the mechanism end to end.
        pricing.MODEL_RATES[("chat_agent", "sarvam 30b")] = pricing.ModelRate(
            "chat_agent", "Sarvam 30b", Decimal("9.00"))
        pricing.MODEL_RATES[("chat_agent", "tiny model")] = pricing.ModelRate(
            "chat_agent", "Tiny Model", Decimal("2.50"), unit_size=1000)

        r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000})
        check("no model -> service rate 4.38", r.json()["data"]["cost"] == 4.38, r.text)
        r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000, "model": "Sarvam 30b"})
        check("model override changes the price (9.0)", r.json()["data"]["cost"] == 9.0 and r.json()["data"]["rate"] == 9.0, r.text)
        r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000, "model": "SARVAM   30b"})
        check("override lookup is case/space insensitive", r.json()["data"]["cost"] == 9.0, r.text)
        r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000, "model": "Unlisted Model"})
        check("unpriced model falls back to service rate", r.json()["data"]["cost"] == 4.38, r.text)
        # unit_size override: 2.50 per 1000 tokens over 10000 tokens = 25.0
        r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000, "model": "Tiny Model"})
        check("per-model unit_size override applies", r.json()["data"]["cost"] == 25.0 and r.json()["data"]["unit_size"] == 1000, r.text)

        before = client.get("/billing", headers=auth).json()["data"]["credits"]
        r = client.post("/usage", headers=headers_key, json={"service": "chat_agent", "quantity": 10000, "model": "Sarvam 30b"})
        check("recorded usage stores the model's rate, not the service's", r.json()["data"]["rate"] == 9.0 and r.json()["data"]["cost"] == 9.0, r.text)
        after = client.get("/billing", headers=auth).json()["data"]["credits"]
        check("credits debited at the model rate", round(before - after, 4) == 9.0, f"{before} -> {after}")

        r = client.get("/pricing")
        chat = next(x for x in r.json()["data"] if x["service"] == "chat_agent")
        check("rate card exposes per-model prices", len(chat["models"]) == 2 and any(m["model"] == "Sarvam 30b" and m["rate"] == 9.0 for m in chat["models"]), str(chat))
        tts = next(x for x in r.json()["data"] if x["service"] == "tts_streaming")
        check("services with no model prices list none", tts["models"] == [], str(tts))

        pricing.MODEL_RATES.clear()

        # --- input/output split pricing ---------------------------------------
        # Ships unset (no invented prices), so switch chat_agent over here.
        flat_chat = pricing.SERVICES["chat_agent"]
        pricing.SERVICES["chat_agent"] = replace(
            flat_chat, input_rate=Decimal("1.50"), output_rate=Decimal("7.50"))
        try:
            r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000})
            check("flat quantity still works on a split service", r.json()["data"]["cost"] == 4.38, r.text)

            # 1.50 x 8000/10000 + 7.50 x 2000/10000 = 1.20 + 1.50 = 2.70
            r = client.post("/usage/estimate", json={"service": "chat_agent", "inputQuantity": 8000, "outputQuantity": 2000})
            j = r.json()["data"]
            check("split pricing charges input and output separately", j["cost"] == 2.7, r.text)
            check("split response reports both rates", j["input_rate"] == 1.5 and j["output_rate"] == 7.5 and j["rate"] is None, str(j))
            check("total quantity derived from the split", j["quantity"] == 10000 and j["pricing"] == "split", str(j))
            # Same 10k tokens, output-heavy: 1.50 x 0.2 + 7.50 x 0.8 = 0.30 + 6.00
            r = client.post("/usage/estimate", json={"service": "chat_agent", "inputQuantity": 2000, "outputQuantity": 8000})
            check("output-heavy usage costs more than input-heavy", r.json()["data"]["cost"] == 6.3, r.text)

            r = client.post("/usage/estimate", json={"service": "stt_streaming", "inputQuantity": 1, "outputQuantity": 1})
            check("split on a flat-only service -> 400", r.status_code == 400 and "not priced separately" in r.json()["detail"], r.text)
            r = client.post("/usage/estimate", json={"service": "chat_agent", "inputQuantity": 100})
            check("one side of the split alone -> 422", r.status_code == 422, r.text)
            r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10, "inputQuantity": 5, "outputQuantity": 5})
            check("both pricing forms at once -> 422", r.status_code == 422, r.text)
            r = client.post("/usage/estimate", json={"service": "chat_agent"})
            check("neither form -> 422", r.status_code == 422, r.text)
            r = client.post("/usage/estimate", json={"service": "chat_agent", "inputQuantity": 0, "outputQuantity": 0})
            check("zero total -> 422", r.status_code == 422, r.text)

            # A flat-rate model override wins over the service's split.
            pricing.MODEL_RATES[("chat_agent", "flat model")] = pricing.ModelRate(
                "chat_agent", "Flat Model", Decimal("9.00"))
            r = client.post("/usage/estimate", json={"service": "chat_agent", "inputQuantity": 5000, "outputQuantity": 5000, "model": "Flat Model"})
            check("flat model override beats the service split -> 400", r.status_code == 400, r.text)
            r = client.post("/usage/estimate", json={"service": "chat_agent", "quantity": 10000, "model": "Flat Model"})
            check("...and prices that model flat", r.json()["data"]["cost"] == 9.0, r.text)
            pricing.MODEL_RATES.clear()

            # Recorded, stored and charged.
            before = client.get("/billing", headers=auth).json()["data"]["credits"]
            r = client.post("/usage", headers=headers_key, json={"service": "chat_agent", "inputQuantity": 8000, "outputQuantity": 2000})
            rec = r.json()["data"]
            check("split usage records both quantities", rec["input_quantity"] == 8000 and rec["output_quantity"] == 2000, r.text)
            check("split usage stores both rates", rec["input_rate"] == 1.5 and rec["output_rate"] == 7.5, r.text)
            after = client.get("/billing", headers=auth).json()["data"]["credits"]
            check("split usage debits the split cost", round(before - after, 4) == 2.7, f"{before} -> {after}")
            r = client.get("/usage/export.csv", headers=auth)
            check("CSV carries the split columns", "input_quantity" in r.text.split("\n")[0], r.text.split("\n")[0])

            r = client.get("/pricing")
            chat = next(x for x in r.json()["data"] if x["service"] == "chat_agent")
            check("rate card advertises split pricing", chat["pricing_mode"] == "split" and chat["input_rate"] == 1.5, str(chat))
            stt = next(x for x in r.json()["data"] if x["service"] == "stt_streaming")
            check("flat services still advertise flat", stt["pricing_mode"] == "flat" and stt["input_rate"] is None, str(stt))
        finally:
            pricing.SERVICES["chat_agent"] = flat_chat

        # --- credit ledger --------------------------------------------------
        r = client.get("/billing/history", headers=auth)
        entries = r.json()["data"]
        billed_records = len([d for d in client.get("/usage?limit=500", headers=auth).json()["data"] if d["billed"]])
        check("ledger has an entry per purchase and per billed spend", len(entries) == billed_records + 1, f"{len(entries)} entries vs {billed_records} billed + 1")
        check("ledger newest first, spends are negative", entries[0]["credits"] < 0 and entries[0]["kind"] == "usage" and entries[-1]["credits"] == 500.0 and entries[-1]["kind"] == "purchase", str(entries[:2]))
        live_balance = client.get("/billing", headers=auth).json()["data"]["credits"]
        check("ledger running balance agrees with the account", entries[0]["balance_after"] == live_balance, f"{entries[0]['balance_after']} vs {live_balance}")
        r = client.get("/billing/history?kind=purchase", headers=auth)
        check("ledger filters by kind", r.json()["count"] == 1, r.text)

        r = client.get("/billing", headers=auth)
        j = r.json()["data"]
        # The invariant, not a snapshot: the balance IS the ledger, summed.
        # Unbilled usage never took credits, so it must not appear here.
        ledger_sum = round(sum(e["credits"] for e in entries), 4)
        check("balance equals the sum of the ledger", j["credits"] == ledger_sum, f"{j['credits']} vs {ledger_sum}")
        check("credits_used = purchased - balance", round(j["lifetime_purchased_credits"] - j["credits"], 4) == j["credits_used"], r.text)

        # --- buying a pack upgrades the tier ---------------------------------
        r = client.post("/billing/top-up", headers=auth, json={"plan": "pro"})
        pro_payment = r.json()["data"]["id"]
        check("pack top-up is priced at 10000 for 11000 credits", r.json()["data"]["credits"] == 11000.0, r.text)
        r = client.post(f"/billing/payments/{pro_payment}/confirm", headers=secret_hdr, json={"providerPaymentId": "pay_2", "method": "Visa •••• 4242"})
        check("pack payment confirmed", r.status_code == 200, r.text)
        r = client.get("/limits", headers=auth)
        check("pack purchase upgrades plan to pro @200rpm", r.json()["data"]["plan"] == "pro" and r.json()["data"]["requests_per_minute"] == 200, r.text)
        r = client.get("/billing", headers=auth)
        check("pack credits added (+11000)", round(r.json()["data"]["credits"] - live_balance, 4) == 11000.0, r.text)
        r = client.get("/billing/payments", headers=auth)
        pays = r.json()["data"]
        check("payments list shows method + status", len(pays) == 2 and pays[0]["status"] == "paid" and pays[0]["method"] == "Visa •••• 4242", r.text)

        # --- billing details + invoices ---------------------------------------
        r = client.get("/billing/details", headers=auth)
        check("billing details start empty", r.status_code == 200 and r.json()["data"] is None, r.text)
        r = client.put("/billing/details", headers=auth, json={
            "legalName": "Acme Global Pvt Ltd", "gstin": "29abcde1234f1z5",
            "addressLine1": "12 MG Road", "city": "Bengaluru",
            "state": "Karnataka", "postalCode": "560001"})
        check("PUT /billing/details saves", r.status_code == 200 and r.json()["data"]["legal_name"] == "Acme Global Pvt Ltd", r.text)
        check("GSTIN normalised to uppercase", r.json()["data"]["gstin"] == "29ABCDE1234F1Z5", r.text)
        check("country defaults to IN", r.json()["data"]["country"] == "IN", r.text)
        r = client.put("/billing/details", headers=auth, json={
            "legalName": "X", "gstin": "not-a-gstin", "addressLine1": "a",
            "city": "b", "state": "c", "postalCode": "d"})
        check("malformed GSTIN -> 422", r.status_code == 422, r.text)

        # An invoice should exist for the two payments already confirmed above.
        r = client.get("/billing/invoices", headers=auth)
        inv = r.json()["data"]
        check("an invoice per confirmed payment", r.json()["count"] == 2, r.text)
        check("invoice numbers are sequential + gap-free", sorted(i["invoice_number"] for i in inv) == ["INV-0001", "INV-0002"], str([i["invoice_number"] for i in inv]))
        r = client.get("/billing/payments", headers=auth)
        check("payment carries its invoice number", all(p["invoice_number"] for p in r.json()["data"]), r.text)

        # A third payment, now that billing details exist.
        r = client.post("/billing/top-up", headers=auth, json={"amountInr": 100})
        pid = r.json()["data"]["id"]
        r = client.post(f"/billing/payments/{pid}/confirm", headers=secret_hdr, json={
            "providerPaymentId": "pay_3", "method": "UPI",
            "providerInvoiceId": "rzp_inv_9", "providerInvoiceUrl": "https://rzp.example/inv/9"})
        issued = r.json()["invoice"]
        check("confirm returns the issued invoice", r.status_code == 200 and issued["invoice_number"] == "INV-0003", r.text)
        check("invoice snapshots the bill-to details", issued["bill_to"]["legal_name"] == "Acme Global Pvt Ltd" and issued["bill_to"]["gstin"] == "29ABCDE1234F1Z5", str(issued["bill_to"]))
        check("invoice flags complete billing details", issued["bill_to"]["complete"] is True, str(issued["bill_to"]))
        check("earlier invoices flagged incomplete", any(i["bill_to"]["complete"] is False for i in inv), str([i["bill_to"] for i in inv]))
        check("gateway invoice reference stored", issued["provider_invoice_url"] == "https://rzp.example/inv/9", str(issued))
        check("tax is explicitly not computed, not zero", issued["tax_status"] == "not_computed", str(issued))
        check("invoice amount matches the payment", issued["amount"] == 100.0 and issued["credits"] == 100.0, str(issued))

        r = client.get("/billing/invoices/INV-0003", headers=auth)
        check("invoice fetchable by number", r.status_code == 200 and r.json()["data"]["id"] == issued["id"], r.text)
        r = client.get(f"/billing/invoices/{issued['id']}", headers=auth)
        check("invoice fetchable by id", r.status_code == 200, r.text)
        r = client.get("/billing/invoices/INV-9999", headers=auth)
        check("unknown invoice -> 404", r.status_code == 404, r.text)
        r = client.get(f"/billing/invoices/{issued['id']}", headers=rival_auth)
        check("cannot read another customer's invoice -> 404", r.status_code == 404, r.text)

        # Immutability: changing the address must not rewrite a past invoice.
        client.put("/billing/details", headers=auth, json={
            "legalName": "Acme Renamed Ltd", "addressLine1": "99 New Street",
            "city": "Mumbai", "state": "Maharashtra", "postalCode": "400001"})
        r = client.get("/billing/invoices/INV-0003", headers=auth)
        check("issued invoice is immutable", r.json()["data"]["bill_to"]["legal_name"] == "Acme Global Pvt Ltd", r.text)

        # A replayed webhook must not mint a second invoice.
        r = client.post(f"/billing/payments/{pid}/confirm", headers=secret_hdr, json={"providerPaymentId": "pay_3"})
        check("replayed confirm issues no second invoice", r.json()["replayed"] is True, r.text)
        r = client.get("/billing/invoices", headers=auth)
        check("still exactly 3 invoices after replay", r.json()["count"] == 3, r.text)

        # --- auto-recharge ----------------------------------------------------
        r = client.put("/billing/auto-recharge", headers=auth, json={"enabled": True, "thresholdCredits": 100, "amountInr": 5000})
        check("auto-recharge saved", r.status_code == 200 and r.json()["data"]["auto_recharge"]["enabled"] is True, r.text)
        check("auto-recharge response admits it is not active", "not active" in r.json()["message"], r.text)
        r = client.put("/billing/auto-recharge", headers=auth, json={"enabled": True})
        check("auto-recharge enabled without settings -> 422", r.status_code == 422, r.text)
        r = client.put("/billing/auto-recharge", headers=auth, json={"enabled": False})
        check("auto-recharge can be disabled", r.json()["data"]["auto_recharge"]["enabled"] is False, r.text)

        # --- the overspend guard ----------------------------------------------
        # Ten concurrent debits of 30 against a balance of 100: exactly three
        # must succeed. Read-then-write would let more through.
        async def overspend_probe():
            uid = ObjectId()
            await credits.grant(uid, credits.to_units(100), credits.KIND_ADJUSTMENT, "probe")
            results = await asyncio.gather(*[
                credits.try_debit(uid, credits.to_units(30), "probe spend") for _ in range(10)
            ])
            granted = [x for x in results if x is not None]
            return len(granted), await credits.balance_units(uid)

        wins, remaining = anyio.run(overspend_probe)
        check("concurrent debits cannot overspend", wins == 3, f"{wins} of 10 succeeded, expected 3")
        check("balance exact after contention", remaining == credits.to_units(10), f"balance_units={remaining}")

        # --- revoke key blocks usage -----------------------------------
        r = client.delete(f"/api-keys/{key_id}", headers=auth)
        check("DELETE /api-keys/{id} revokes", r.status_code == 200, r.text)
        r = client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 1})
        check("POST /usage with revoked key -> 401", r.status_code == 401, r.text)

        # --- forgot / reset password -----------------------------------
        outbox.clear()
        r = client.post("/auth/forgot-password", json={"email": "ops@acme.io"})
        reset_token = r.json().get("reset_token")  # exposed in dev
        check("POST /auth/forgot-password returns dev token", r.status_code == 200 and bool(reset_token), r.text)

        # --- the reset email itself -------------------------------------
        check("forgot-password sends exactly one email", len(outbox) == 1, str(outbox))
        mail = outbox[0] if outbox else {}
        check("reset email goes to the account address", mail.get("to") == "ops@acme.io", str(mail))
        check("reset email carries the working link", f"https://app.example.com/reset-password?token={reset_token}" in mail.get("body", ""), str(mail))
        check("reset email states the expiry", "30 minutes" in mail.get("body", ""), str(mail))
        check("reset email has a clear subject", mail.get("subject") == "Reset your ChatBucket password", str(mail))

        # The memory backend stores the body pre-encoding, so encode it the way
        # the SMTP backend would and read it back as a mail client does. A
        # reset link that survives everything except MIME is still a dead link.
        built = _build_message(
            mail.get("to", ""), mail.get("subject", ""), mail.get("body", ""), mail.get("html"), None
        )
        text_part = built.get_body(preferencelist=("plain",))
        html_part = built.get_body(preferencelist=("html",))
        decoded = text_part.get_content()
        check("link survives MIME encode/decode", f"?token={reset_token}" in decoded, decoded[:400])
        check("designed HTML travels as the alternative part", html_part is not None and "<!DOCTYPE html>" in html_part.get_content(), str(built.get_content_type()))
        # The templates carry rupee signs, em dashes and emoji. Left to choose,
        # Python encodes those 8bit, which a server without 8BITMIME may reject.
        for part in built.walk():
            if part.get_content_maintype() == "text":
                check(
                    f"{part.get_content_subtype()} part stays 7-bit safe (no 8BITMIME needed)",
                    part["Content-Transfer-Encoding"] != "8bit",
                    str(part["Content-Transfer-Encoding"]),
                )
        check("From header uses the configured sender", built["From"] == "ChatBucket <support@chatbucket.business>", str(built["From"]))
        r = client.post("/auth/reset-password", json={"token": reset_token, "new_password": "finalsecret9"})
        check("POST /auth/reset-password ok", r.status_code == 200, r.text)
        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"})
        check("login with reset password", r.status_code == 200, r.text)
        r = client.post("/auth/reset-password", json={"token": reset_token, "new_password": "another123"})
        check("reused reset token -> 400", r.status_code == 400, r.text)

        # --- refresh tokens + logout -------------------------------------
        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"})
        j = r.json()
        refresh_1 = j.get("refresh_token")
        check("login returns a refresh token", bool(refresh_1) and bool(j.get("refresh_expires_at")), r.text)

        r = client.post("/auth/refresh", json={"refreshToken": refresh_1})
        j2 = r.json()
        refresh_2 = j2.get("refresh_token")
        check("POST /auth/refresh issues a new access token", r.status_code == 200 and bool(j2.get("access_token")), r.text)
        check("refresh token is rotated, not reused", refresh_2 and refresh_2 != refresh_1, r.text)
        r = client.get("/profile", headers={"Authorization": f"Bearer {j2['access_token']}"})
        check("refreshed access token works", r.status_code == 200, r.text)

        # Replaying a spent token means it leaked: revoke the whole family.
        r = client.post("/auth/refresh", json={"refreshToken": refresh_1})
        check("reusing a spent refresh token -> 401", r.status_code == 401, r.text)
        r = client.post("/auth/refresh", json={"refreshToken": refresh_2})
        check("reuse revokes the whole session family", r.status_code == 401, r.text)
        r = client.post("/auth/refresh", json={"refreshToken": "not-a-token"})
        check("unknown refresh token -> 401", r.status_code == 401, r.text)

        # Logout
        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"})
        sess = r.json()
        sess_auth = {"Authorization": f"Bearer {sess['access_token']}"}
        r = client.post("/auth/logout", headers=sess_auth, json={"refreshToken": sess["refresh_token"]})
        check("POST /auth/logout ok", r.status_code == 200, r.text)
        r = client.post("/auth/refresh", json={"refreshToken": sess["refresh_token"]})
        check("revoked refresh token cannot renew -> 401", r.status_code == 401, r.text)
        r = client.post("/auth/logout", json={"refreshToken": "x"})
        check("logout needs auth -> 401", r.status_code == 401, r.text)

        # Logout everywhere also retires live access tokens.
        a = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"}).json()
        b = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"}).json()
        r = client.post("/auth/logout", headers={"Authorization": f"Bearer {a['access_token']}"}, json={"allSessions": True})
        check("logout all reports revoked sessions", r.status_code == 200 and r.json()["sessions_revoked"] >= 2, r.text)
        r = client.post("/auth/refresh", json={"refreshToken": b["refresh_token"]})
        check("other session's refresh token is dead", r.status_code == 401, r.text)
        r = client.get("/profile", headers={"Authorization": f"Bearer {b['access_token']}"})
        check("logout all retires live access tokens too", r.status_code == 401, r.text)

        # Re-establish a working session for the rest of the suite.
        j = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"}).json()
        auth = {"Authorization": f"Bearer {j['access_token']}"}

        # --- rate limiting -------------------------------------------------
        os.environ["RATE_LIMIT_ENABLED"] = "true"
        get_settings.cache_clear()
        real_clock = freeze_limiter_clock()
        try:
            # login_email allows 5 per 15 min against one account.
            codes = [client.post("/auth/login", json={"email": "rl@acme.io", "password": "nope"}).status_code
                     for _ in range(8)]
            check("brute force is rate limited", codes.count(429) >= 2, str(codes))
            check("first attempts still answer normally", codes[0] == 401, str(codes))
            r = client.post("/auth/login", json={"email": "rl@acme.io", "password": "nope"})
            check("429 carries Retry-After", r.status_code == 429 and int(r.headers.get("Retry-After", 0)) > 0, str(dict(r.headers)))
            # A different account is unaffected — the limit is per-email.
            r = client.post("/auth/login", json={"email": "other@acme.io", "password": "nope"})
            check("per-email limit does not block other accounts", r.status_code == 401, r.text)
            # Plan limits: starter is 60/min per service, shared across keys.
            # A fresh key — the one used earlier was revoked by the revoke test.
            live_key = client.post("/api-keys", headers=auth, json={"name": "Limits"}).json()["api_key"]
            headers_live = {"X-API-Key": live_key}
            # Read the limit rather than hardcoding it — this account was
            # upgraded to Pro by the pack-purchase test earlier.
            rpm = client.get("/limits", headers=auth).json()["data"]["requests_per_minute"]
            r = client.post("/usage", headers=headers_live, json={"service": "stt_offline", "quantity": 1})
            check("metered call reports its rate-limit headers", r.headers.get("X-RateLimit-Limit") == str(rpm) and int(r.headers.get("X-RateLimit-Remaining", -1)) >= 0, str(dict(r.headers)))
            codes = [client.post("/usage", headers=headers_live, json={"service": "stt_offline", "quantity": 0.001}).status_code
                     for _ in range(rpm + 5)]
            check("plan rate limit is enforced on POST /usage", 429 in codes, f"{codes.count(429)} of {rpm + 5} throttled")
            throttled = client.post("/usage", headers=headers_live, json={"service": "stt_offline", "quantity": 0.001})
            check("429 carries plan limit headers", throttled.status_code == 429 and throttled.headers.get("X-RateLimit-Limit") == str(rpm) and int(throttled.headers.get("Retry-After", 0)) > 0, str(dict(throttled.headers)))
            # A different service has its own allowance.
            r = client.post("/usage", headers=headers_live, json={"service": "tts_offline", "quantity": 1})
            check("plan limit is per service, not account-wide", r.status_code == 201, r.text)

            # forgot-password is capped per address, so it cannot mail-bomb.
            codes = [client.post("/auth/forgot-password", json={"email": "ops@acme.io"}).status_code
                     for _ in range(5)]
            check("forgot-password is rate limited per address", 429 in codes, str(codes))
        finally:
            ratelimit.time = real_clock
            os.environ["RATE_LIMIT_ENABLED"] = "false"
            get_settings.cache_clear()

        r = client.post("/auth/login", json={"email": "rl@acme.io", "password": "nope"})
        check("limits lift when disabled", r.status_code == 401, r.text)

        # --- forgot-password for unknown email does not leak -----------
        outbox.clear()
        r = client.post("/auth/forgot-password", json={"email": "ghost@nowhere.io"})
        check("forgot-password unknown email still 200, no token", r.status_code == 200 and "reset_token" not in r.json(), r.text)
        check("unknown email triggers no mail", outbox == [], str(outbox))

        # --- email verification ------------------------------------------
        r = client.post("/auth/verify-email", json={"token": "nope"})
        check("bad verification token -> 400", r.status_code == 400, r.text)
        r = client.post("/auth/verify-email", json={"token": verify_token})
        check("POST /auth/verify-email confirms the address", r.status_code == 200, r.text)
        r = client.get("/profile", headers=auth)
        check("profile reports verified", r.json()["data"]["email_verified"] is True, r.text)
        r = client.post("/auth/verify-email", json={"token": verify_token})
        check("verification token is single-use -> 400", r.status_code == 400, r.text)
        r = client.post("/auth/verify-email/resend", headers=auth)
        check("resend on a verified account is a no-op", r.status_code == 200 and "already" in r.json()["message"], r.text)
        r = client.post("/auth/verify-email/resend")
        check("resend needs auth -> 401", r.status_code == 401, r.text)

        # Gating key creation on verification.
        os.environ["REQUIRE_EMAIL_VERIFICATION"] = "true"
        get_settings.cache_clear()
        try:
            r = client.post("/api-keys", headers=auth, json={"name": "Verified"})
            check("verified account can still create keys", r.status_code == 201, r.text)
            r = client.post("/api-keys", headers=rival_auth, json={"name": "Blocked"})
            check("unverified account cannot create keys -> 403", r.status_code == 403, r.text)
        finally:
            os.environ["REQUIRE_EMAIL_VERIFICATION"] = "false"
            get_settings.cache_clear()

        # --- account export ------------------------------------------------
        r = client.get("/account/export", headers=auth)
        j = r.json()["data"]
        check("GET /account/export returns every section", r.status_code == 200 and {"profile", "api_keys", "usage", "credit_ledger", "payments", "invoices", "projects"} <= set(j), str(list(j)))
        check("export hides the password hash", "password_hash" not in j["profile"], str(j["profile"]))
        check("export masks API keys", all(k["masked_key"].startswith("cb_live_****") for k in j["api_keys"]), str(j["api_keys"][:1]))
        check("export carries usage and invoices", len(j["usage"]) > 0 and len(j["invoices"]) == 3, f"{len(j['usage'])} usage, {len(j['invoices'])} invoices")
        r = client.get("/account/export")
        check("export needs auth -> 401", r.status_code == 401, r.text)

        # --- account deletion ------------------------------------------------
        r = client.post("/account/delete", headers=rival_auth, json={"password": "wrong"})
        check("delete with wrong password -> 400", r.status_code == 400, r.text)
        r = client.post("/account/delete", headers=rival_auth, json={"password": "supersecret1"})
        check("POST /account/delete closes the account", r.status_code == 200 and r.json()["api_keys_revoked"] >= 1, r.text)
        check("deletion reports what it retained", "invoices" in r.json()["retained"], r.text)
        r = client.get("/profile", headers=rival_auth)
        check("deleted account's tokens are dead", r.status_code == 401, r.text)
        r = client.post("/auth/login", json={"email": "rival@other.io", "password": "supersecret1"})
        check("deleted account cannot sign in", r.status_code == 401, r.text)
        # The freed address must be reusable by a genuine future signup.
        r = client.post("/auth/register", json={"name": "New Owner", "email": "rival@other.io", "password": "supersecret1", "mobile": "+919000000002", "acceptTerms": True})
        check("deleted account's email can be reused", r.status_code == 201, r.text)

        async def read_closed():
            doc = await database.users_collection().find_one({"name": "Deleted account"})
            kept = await database.usage_collection().count_documents({"user_id": doc["_id"]}) if doc else 0
            return doc, kept

        anon, kept_usage = anyio.run(read_closed)
        check("closed account is anonymised, not erased", anon is not None and anon.get("phone") is None and anon["email"].endswith("@deleted.invalid"), str(anon and anon.get("email")))
        check("financial records survive closure", kept_usage >= 0 and anon.get("deleted_at") is not None, str(anon and anon.get("deleted_at")))

        # --- Razorpay ----------------------------------------------------------
        os.environ["RAZORPAY_KEY_ID"] = "rzp_test_fake"
        os.environ["RAZORPAY_KEY_SECRET"] = "test_key_secret"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret"
        get_settings.cache_clear()

        # Signature verification is pure HMAC — assert it without a network.
        good_sig = hmac.new(b"test_key_secret", b"order_x|pay_x", hashlib.sha256).hexdigest()
        check("valid checkout signature verifies", payments.verify_checkout_signature("order_x", "pay_x", good_sig), good_sig)
        check("tampered payment id fails", not payments.verify_checkout_signature("order_x", "pay_OTHER", good_sig), "")
        check("tampered order id fails", not payments.verify_checkout_signature("order_OTHER", "pay_x", good_sig), "")
        check("empty signature fails", not payments.verify_checkout_signature("order_x", "pay_x", ""), "")
        raw = b'{"event":"payment.captured"}'
        hook_sig = hmac.new(b"test_webhook_secret", raw, hashlib.sha256).hexdigest()
        check("valid webhook signature verifies", payments.verify_webhook_signature(raw, hook_sig), "")
        check("webhook signed with the KEY secret is rejected", not payments.verify_webhook_signature(raw, hmac.new(b"test_key_secret", raw, hashlib.sha256).hexdigest()), "the two secrets must not be interchangeable")
        check("altered webhook body fails", not payments.verify_webhook_signature(raw + b" ", hook_sig), "")

        # Order creation, with the gateway call stubbed.
        created_orders = []

        async def fake_order(amount, currency, receipt):
            created_orders.append((amount, currency, receipt))
            return {"id": f"order_{len(created_orders)}", "amount": int(amount * 100), "currency": currency}

        real_create = payments.create_order
        payments.create_order = fake_order
        try:
            r = client.post("/billing/top-up", headers=auth, json={"amountInr": 500})
            j = r.json()
            pay_id = j["data"]["id"]
            check("top-up returns Razorpay checkout params", j["checkout"]["provider"] == "razorpay" and j["checkout"]["order_id"] == "order_1", str(j.get("checkout")))
            check("checkout amount is in paise", j["checkout"]["amount"] == 50000, str(j["checkout"]))
            check("checkout exposes only the public key id", "key_id" in j["checkout"] and "key_secret" not in str(j["checkout"]), str(j["checkout"]))
            check("payment records the order id", j["data"]["provider_order_id"] == "order_1", r.text)

            # Verifying the checkout callback.
            sig = hmac.new(b"test_key_secret", b"order_1|rzp_pay_1", hashlib.sha256).hexdigest()
            r = client.post(f"/billing/payments/{pay_id}/verify", headers=auth, json={"razorpayOrderId": "order_1", "razorpayPaymentId": "rzp_pay_1", "razorpaySignature": "forged"})
            check("forged checkout signature -> 400", r.status_code == 400, r.text)
            r = client.post(f"/billing/payments/{pay_id}/verify", headers=auth, json={"razorpayOrderId": "order_MISMATCH", "razorpayPaymentId": "rzp_pay_1", "razorpaySignature": sig})
            check("order id from another payment -> 400", r.status_code == 400, r.text)
            before = client.get("/billing", headers=auth).json()["data"]["credits"]
            r = client.post(f"/billing/payments/{pay_id}/verify", headers=auth, json={"razorpayOrderId": "order_1", "razorpayPaymentId": "rzp_pay_1", "razorpaySignature": sig})
            check("valid checkout callback settles the payment", r.status_code == 200 and r.json()["replayed"] is False, r.text)
            after = client.get("/billing", headers=auth).json()["data"]["credits"]
            check("checkout callback grants the credits", round(after - before, 4) == 500.0, f"{before} -> {after}")
            check("checkout callback issues an invoice", r.json()["invoice"] is not None, r.text)

            # The webhook for the same order must not credit twice.
            body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "rzp_pay_1", "order_id": "order_1", "method": "upi"}}}}).encode()
            r = client.post("/billing/webhook/razorpay", content=body, headers={"X-Razorpay-Signature": hmac.new(b"test_webhook_secret", body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"})
            check("webhook after callback is a replay, not a second credit", r.status_code == 200 and r.json()["replayed"] is True, r.text)
            final = client.get("/billing", headers=auth).json()["data"]["credits"]
            check("balance unchanged by the replayed webhook", final == after, f"{after} -> {final}")

            # Webhook-only settlement (customer closed the browser).
            r = client.post("/billing/top-up", headers=auth, json={"amountInr": 250})
            order2 = r.json()["data"]["provider_order_id"]
            body2 = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "rzp_pay_2", "order_id": order2, "method": "card"}}}}).encode()
            r = client.post("/billing/webhook/razorpay", content=body2, headers={"X-Razorpay-Signature": hmac.new(b"test_webhook_secret", body2, hashlib.sha256).hexdigest()})
            check("webhook alone settles a payment", r.status_code == 200 and r.json()["replayed"] is False, r.text)
            check("webhook records the payment method", r.json()["data"]["method"] == "card", r.text)

            r = client.post("/billing/webhook/razorpay", content=body2, headers={"X-Razorpay-Signature": "forged"})
            check("forged webhook signature -> 401", r.status_code == 401, r.text)
            unknown = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "p", "order_id": "order_unknown"}}}}).encode()
            r = client.post("/billing/webhook/razorpay", content=unknown, headers={"X-Razorpay-Signature": hmac.new(b"test_webhook_secret", unknown, hashlib.sha256).hexdigest()})
            check("unknown order acknowledged, not retried forever", r.status_code == 200 and r.json().get("ignored") == "unknown_order", r.text)
            other = json.dumps({"event": "payment.failed", "payload": {"payment": {"entity": {"id": "p", "order_id": order2}}}}).encode()
            r = client.post("/billing/webhook/razorpay", content=other, headers={"X-Razorpay-Signature": hmac.new(b"test_webhook_secret", other, hashlib.sha256).hexdigest()})
            check("unrelated event acknowledged and ignored", r.status_code == 200 and r.json().get("ignored") == "payment.failed", r.text)
        finally:
            payments.create_order = real_create
            for key in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
                os.environ.pop(key, None)
            get_settings.cache_clear()

        r = client.post("/billing/webhook/razorpay", content=b"{}", headers={"X-Razorpay-Signature": "x"})
        check("webhook 503s when Razorpay is not configured", r.status_code == 503, r.text)

        # --- per-second audio billing ----------------------------------------
        flat_voip = pricing.SERVICES["voip_call"]
        pricing.SERVICES["voip_call"] = replace(flat_voip, billing_increment=pricing.SECOND)
        try:
            # 12.3s rounds up to 13s: 5.00 x 13/60 = 1.0833
            r = client.post("/usage/estimate", json={"service": "voip_call", "quantity": 12.3 / 60})
            check("audio rounds up to the next whole second", r.json()["data"]["cost"] == 1.0833, r.text)
            r = client.post("/usage/estimate", json={"service": "voip_call", "quantity": 3})
            check("whole minutes are unaffected by the increment", r.json()["data"]["cost"] == 15.0, r.text)
            r = client.get("/pricing")
            voip = next(x for x in r.json()["data"] if x["service"] == "voip_call")
            check("rate card advertises the billing increment", voip["billing_increment"] is not None, str(voip))
        finally:
            pricing.SERVICES["voip_call"] = flat_voip
        r = client.post("/usage/estimate", json={"service": "voip_call", "quantity": 12.3 / 60})
        check("without an increment, fractions bill exactly", r.json()["data"]["cost"] == 1.025, r.text)

        # --- engine capacity burn --------------------------------------------
        # Our site serves STT/TTS to a signed-in customer on our own engine
        # allowance. The customer is billed at our rate card; the engine's
        # meter is recorded separately as our cost.
        #
        # A fresh account: the one above was closed by the deletion block, and
        # its key and token died with it.
        r = client.post("/auth/register", json={
            "name": "Engine Co", "email": "engine@acme.io", "password": "hunter2secret",
            "mobile": "+919876500123", "acceptTerms": True,
        })
        eng_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        # The plaintext key is top-level and shown exactly once.
        eng_key = {"X-API-Key": client.post(
            "/api-keys", headers=eng_auth, json={"name": "Site"}
        ).json()["api_key"]}
        # Credits so the metered calls below bill rather than 402.
        r = client.post("/billing/top-up", headers=eng_auth, json={"amountInr": 500})
        client.post(
            f"/billing/payments/{r.json()['data']['id']}/confirm",
            headers={"X-Billing-Secret": "test-webhook-secret"},
            json={"providerPaymentId": "pay_engine_1"},
        )

        headers_key = eng_key
        auth = eng_auth
        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 2, "engine": "cb_vinu", "engineQuantity": 2.4,
        })
        check("usage accepts an engine + engine quantity", r.status_code == 201, r.text)
        check("the customer is still billed at OUR rate", r.json()["data"]["cost"] == 0.78, r.text)
        check("engine is not echoed to the caller", "engine" not in r.json()["data"], r.text)

        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 1, "engine": "cb_unknown", "engineQuantity": 1,
        })
        check("unknown engine -> 422, not a silent row", r.status_code == 422, r.text)
        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 1, "engine": "cb_vinu",
        })
        check("engine without its quantity -> 422", r.status_code == 422, r.text)

        # A misspelt key must not be dropped in silence: the call would return
        # 201 having billed the customer correctly while the engine allowance
        # under-reports, which is the exact failure these fields exist to stop.
        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 1,
            "engine": "cb_vinu", "engineQty": 2.4,
        })
        check("misspelt engine field -> 422, not a silent drop", r.status_code == 422, r.text)
        check("the rejection names the offending key", "engineQty" in r.text, r.text)

        # --- provider (operator-only reconciliation) --------------------------
        # Which upstream served the call, so a capability served by two of them
        # can be split across two invoices.
        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 3,
            "engine": "cb_vinu", "engineQuantity": 3.1, "provider": "provider-one",
        })
        check("usage accepts a provider", r.status_code == 201, r.text)
        check("provider is not echoed to the caller", "provider" not in r.json()["data"], r.text)
        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 2,
            "engine": "cb_vinu", "engineQuantity": 2.2, "provider": "provider-two",
        })
        check("a second provider on the same engine", r.status_code == 201, r.text)
        r = client.post("/usage", headers=headers_key, json={
            "service": "stt_offline", "quantity": 1, "provider": "provider-one",
        })
        check("provider without an engine -> 422", r.status_code == 422, r.text)

        # A signed-in customer has a session, not an API key — the key was shown
        # once and stored hashed, so the site cannot produce it server-side.
        r = client.post("/usage", headers=auth, json={
            "service": "tts_streaming", "quantity": 1000, "engine": "cb_paluku", "engineQuantity": 1200,
        })
        check("usage can be metered with a bearer token", r.status_code == 201, r.text)
        check("token-metered usage bills normally", r.json()["data"]["cost"] == 0.91, r.text)
        r = client.get("/usage", headers=auth)
        no_key = [d for d in r.json()["data"] if d.get("api_key_id") is None]
        check("token-metered usage claims no API key", len(no_key) >= 1, r.text)
        r = client.post("/usage", json={"service": "stt_offline", "quantity": 1})
        check("metering with neither credential -> 401", r.status_code == 401, r.text)

        # The customer must not see who serves their calls, or what it costs us.
        r = client.get("/usage", headers=auth)
        check(
            "engine never appears in customer usage history",
            all("engine" not in d and "engine_quantity" not in d for d in r.json()["data"]),
            r.text,
        )

        # --- engine burn, operator view ---------------------------------------
        r = client.get("/engines/usage")
        check("engine view 503s when OPS_SECRET is unset", r.status_code == 503, r.text)
        os.environ["OPS_SECRET"] = "test-ops-secret"
        get_settings.cache_clear()
        try:
            r = client.get("/engines/usage")
            check("engine view without the secret -> 401", r.status_code == 401, r.text)
            r = client.get("/engines/usage", headers={"X-Ops-Secret": "wrong"})
            check("engine view with a wrong secret -> 401", r.status_code == 401, r.text)

            ops = {"X-Ops-Secret": "test-ops-secret"}
            r = client.get("/engines/usage", headers=ops)
            j = r.json()
            check("engine view returns 200 with the secret", r.status_code == 200, r.text)
            rows = {row["engine"]: row for row in j["data"]}
            check("cb_vinu burn is counted in ITS unit", rows["cb_vinu"]["consumed"] == 7.7, str(rows))  # 2.4 + 3.1 + 2.2
            check("cb_paluku burn is counted separately", rows["cb_paluku"]["consumed"] == 1200, str(rows))
            check("burn is reported in the engine's unit", rows["cb_paluku"]["unit"] == "characters", str(rows))
            check("the burning account is named", rows["cb_vinu"]["top_accounts"][0]["email"] == "engine@acme.io", str(rows))

            # One engine, two upstreams: the split is what each invoice covers.
            split = {p["provider"]: p["consumed"] for p in rows["cb_vinu"]["by_provider"]}
            check("engine burn splits by provider", split.get("provider-one") == 3.1 and split.get("provider-two") == 2.2, str(split))
            check(
                "unattributed burn is named, not dropped",
                "(unreported)" in split,
                str(split),
            )
            check(
                "the provider split adds up to the engine total",
                abs(sum(split.values()) - rows["cb_vinu"]["consumed"]) < 0.0001,
                f"{split} vs {rows['cb_vinu']['consumed']}",
            )

            # Nothing has told us how big the free tier is, so nothing is claimed.
            check("remaining is null while no quota is set", rows["cb_vinu"]["remaining"] is None, str(rows))
            check("percent_used is null too, not 0", rows["cb_vinu"]["percent_used"] is None, str(rows))
            check("and it does not claim to be exhausted", rows["cb_vinu"]["exhausted"] is False, str(rows))
            check("the view says quotas are unconfigured", j["quotas_configured"] is False, r.text)

            os.environ["ENGINE_FREE_QUOTAS"] = "cb_vinu=10,cb_paluku=1000"
            get_settings.cache_clear()
            r = client.get("/engines/usage", headers=ops)
            j = r.json()
            rows = {row["engine"]: row for row in j["data"]}
            check("configured quota is reported", rows["cb_vinu"]["free_quota"] == 10, str(rows))
            check("remaining = quota - consumed", rows["cb_vinu"]["remaining"] == 2.3, str(rows))  # 10 - 7.7
            check("percent_used is computed", rows["cb_vinu"]["percent_used"] == 77.0, str(rows))  # 7.7 / 10
            check("the view says quotas are configured", j["quotas_configured"] is True, r.text)
            # CB Paluku burned 1200 of 1000 - over the allowance.
            check("an overrun reads exhausted", rows["cb_paluku"]["exhausted"] is True, str(rows))
            check("remaining floors at 0, never negative", rows["cb_paluku"]["remaining"] == 0.0, str(rows))
            check("percent_used caps at 100", rows["cb_paluku"]["percent_used"] == 100.0, str(rows))

            # An engine nobody reported for must still be listed: absent and
            # idle look identical otherwise, and one of them is an outage.
            os.environ["ENGINE_FREE_QUOTAS"] = ""
            get_settings.cache_clear()
            r = client.get("/engines/usage", headers=ops)
            check(
                "every known engine is listed, even with no traffic",
                {row["engine"] for row in r.json()["data"]} >= {"cb_vinu", "cb_paluku"},
                r.text,
            )
        finally:
            for key in ("OPS_SECRET", "ENGINE_FREE_QUOTAS"):
                os.environ.pop(key, None)
            get_settings.cache_clear()

        # --- email verification by code ---------------------------------
        outbox.clear()
        r = client.post("/auth/register", json={
            "name": "Otp Tester", "email": "otp@acme.io", "password": "supersecret1",
            "mobile": "+919000000009", "acceptTerms": True,
        })
        otp_code = r.json().get("verification_code")
        otp_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        check("register issues a six-digit code", (otp_code or "").isdigit() and len(otp_code) == 6, r.text)

        r = client.post("/auth/verify-email/otp", json={"email": "otp@acme.io", "code": "000000" if otp_code != "000000" else "111111"})
        check("wrong code -> 400", r.status_code == 400, r.text)
        r = client.post("/auth/verify-email/otp", json={"email": "nobody@nowhere.io", "code": otp_code})
        check("code for an unknown address -> 400", r.status_code == 400, r.text)
        r = client.post("/auth/verify-email/otp", json={"email": "otp@acme.io", "code": "12345"})
        check("a five-digit code -> 422", r.status_code == 422, r.text)
        outbox.clear()
        r = client.post("/auth/verify-email/otp", json={"email": "otp@acme.io", "code": otp_code})
        check("POST /auth/verify-email/otp confirms the address", r.status_code == 200, r.text)
        r = client.get("/profile", headers=otp_auth)
        check("profile reports verified after the code", r.json()["data"]["email_verified"] is True, r.text)

        # Verifying is a real change in what the account can do, not a
        # formality, so it gets its own confirmation.
        confirmation = next((m for m in outbox if m["to"] == "otp@acme.io"), {})
        check("verifying sends a confirmation", confirmation.get("subject") == "Your ChatBucket email is verified", str([m["subject"] for m in outbox]))
        check("the confirmation carries the designed HTML", (confirmation.get("html") or "").startswith("<!DOCTYPE"), str(confirmation.get("html"))[:60])
        check("confirmation HTML has no unrendered placeholders", "{{" not in (confirmation.get("html") or ""), str(confirmation.get("html"))[:60])
        check("the confirmation replies to support", confirmation.get("reply_to") == "support@chatbucket.business", str(confirmation.get("reply_to")))

        outbox.clear()
        r = client.post("/auth/verify-email/otp", json={"email": "otp@acme.io", "code": otp_code})
        check("re-verifying sends no second confirmation", outbox == [], str([m["subject"] for m in outbox]))
        r = client.post("/auth/verify-email/otp", json={"email": "otp@acme.io", "code": otp_code})
        check("re-verifying is idempotent, not an error", r.status_code == 200 and "already" in r.json()["message"], r.text)

        # The code is spent, so the link that came with it must be dead too.
        r = client.post("/auth/register", json={
            "name": "Both Creds", "email": "both@acme.io", "password": "supersecret1",
            "mobile": "+919000000010", "acceptTerms": True,
        })
        both = r.json()
        r = client.post("/auth/verify-email/otp", json={"email": "both@acme.io", "code": both["verification_code"]})
        check("verifying by code succeeds", r.status_code == 200, r.text)
        r = client.post("/auth/verify-email", json={"token": both["verification_token"]})
        check("using the code retires the link too", r.status_code == 400, r.text)

        # Guessing is capped in the database, so the cap survives a restart and
        # holds across workers.
        r = client.post("/auth/register", json={
            "name": "Brute Force", "email": "brute@acme.io", "password": "supersecret1",
            "mobile": "+919000000011", "acceptTerms": True,
        })
        brute_code = r.json()["verification_code"]
        wrong = "000000" if brute_code != "000000" else "111111"
        codes = [client.post("/auth/verify-email/otp", json={"email": "brute@acme.io", "code": wrong}).status_code for _ in range(6)]
        check("wrong codes are refused", codes[:5] == [400] * 5, str(codes))
        check("too many wrong codes burns the code -> 429", codes[5] == 429, str(codes))
        r = client.post("/auth/verify-email/otp", json={"email": "brute@acme.io", "code": brute_code})
        check("the real code no longer works after the cap", r.status_code == 429, r.text)

        # --- the deposit receipt -----------------------------------------
        outbox.clear()
        # Whichever account `auth` is by this point in the run — the receipt
        # must go to the holder of the account that was topped up, not to
        # whoever this block happens to have been written around.
        payer_email = client.get("/profile", headers=auth).json()["data"]["email"]
        r = client.post("/billing/top-up", headers=auth, json={"amountInr": 250})
        receipt_payment = r.json()["data"]["id"]
        r = client.post(f"/billing/payments/{receipt_payment}/confirm", headers=secret_hdr, json={"providerPaymentId": "pay_receipt", "method": "upi"})
        check("top-up confirmed for the receipt test", r.status_code == 200, r.text)
        receipt = next((m for m in outbox if m["subject"].startswith("Deposit successful")), {})
        check("a confirmed payment emails a receipt", bool(receipt), str([m["subject"] for m in outbox]))
        check("receipt goes to the account holder", receipt.get("to") == payer_email, str(receipt.get("to")))
        check("receipt states the amount", "250.00" in receipt.get("body", ""), receipt.get("body", ""))
        check("receipt names the gateway payment", "pay_receipt" in receipt.get("body", ""), receipt.get("body", ""))
        check("receipt carries the designed HTML", (receipt.get("html") or "").startswith("<!DOCTYPE html>"), str(receipt.get("html"))[:80])
        check("receipt HTML has no unrendered placeholders", "{{" not in (receipt.get("html") or ""), str(receipt.get("html"))[:80])

        outbox.clear()
        r = client.post(f"/billing/payments/{receipt_payment}/confirm", headers=secret_hdr, json={"providerPaymentId": "pay_receipt"})
        check("a redelivered webhook reads as a replay", r.json()["replayed"] is True, r.text)
        check("a replayed confirmation sends no second receipt", outbox == [], str(outbox))

        # --- operator broadcasts ------------------------------------------
        os.environ["OPS_SECRET"] = "ops-secret-value"
        get_settings.cache_clear()
        try:
            ops = {"X-Ops-Secret": "ops-secret-value"}
            announcement = {
                "subject": "ChatBucket news", "headline": "Something happened",
                "summary": "A short summary of the thing that happened.",
                "highlights": ["One", "Two"],
            }

            r = client.post("/notifications/announcement", json={**announcement, "confirm": True})
            check("announcement without the ops secret -> 401", r.status_code == 401, r.text)
            r = client.post("/notifications/announcement", headers=ops, json=announcement)
            check("announcement with neither confirm nor testEmail -> 422", r.status_code == 422, r.text)

            outbox.clear()
            r = client.post("/notifications/announcement", headers=ops, json={**announcement, "testEmail": "reviewer@acme.io"})
            check("a test send goes to one address only", r.status_code == 200 and len(outbox) == 1, str(outbox))
            check("the test send says it recorded nothing", r.json()["preview"] is True, r.text)
            check("the test copy reaches the reviewer", outbox[0]["to"] == "reviewer@acme.io", str(outbox[0]))
            check("announcement HTML has no unrendered placeholders", "{{" not in (outbox[0].get("html") or ""), str(outbox[0].get("html"))[:80])
            preview_reference = r.json()["reference_id"]

            outbox.clear()
            r = client.post("/notifications/announcement", headers=ops, json={**announcement, "confirm": True, "referenceId": "ANN-TEST-1"})
            first = r.json()["data"]
            check("a confirmed announcement reaches verified accounts", r.status_code == 200 and first["sent"] >= 1, r.text)
            check("unverified accounts are left out", first["sent"] <= first["considered"], r.text)
            check("every recipient got the designed HTML", all("{{" not in (m.get("html") or "x") for m in outbox), str(len(outbox)))
            sent_first = first["sent"]

            r = client.post("/notifications/announcement", headers=ops, json={**announcement, "confirm": True, "referenceId": "ANN-TEST-1"})
            repeat = r.json()["data"]
            check("re-running the same announcement mails nobody twice", repeat["sent"] == 0 and repeat["skipped"] == sent_first, r.text)
            check("a preview does not consume the reference id", preview_reference != "ANN-TEST-1", preview_reference)

            outbox.clear()
            r = client.post("/notifications/maintenance", headers=ops, json={
                "subject": "Scheduled maintenance",
                "startsAt": "2026-09-01T02:00:00Z",
                "endsAt": "2026-09-01T04:00:00Z",
                "confirm": True,
            })
            check("maintenance notice sends", r.status_code == 200 and r.json()["data"]["sent"] >= 1, r.text)
            check("maintenance reaches unverified accounts too", r.json()["data"]["sent"] >= sent_first, r.text)
            check("maintenance HTML has no unrendered placeholders", all("{{" not in (m.get("html") or "x") for m in outbox), str(len(outbox)))

            r = client.post("/notifications/maintenance", headers=ops, json={
                "subject": "Backwards", "startsAt": "2026-09-01T04:00:00Z",
                "endsAt": "2026-09-01T02:00:00Z", "confirm": True,
            })
            check("a window that ends before it starts -> 422", r.status_code == 422, r.text)

            outbox.clear()
            r = client.post("/notifications/onboarding-nudges", headers=ops)
            check("the nudge run reports what it did", r.status_code == 200 and "sent" in r.json()["data"], r.text)

            # --- chasing accounts that never verified ---------------------
            # With REQUIRE_EMAIL_VERIFICATION on, an unverified account is
            # stuck: credited, signed in, and unable to create a key.
            client.post("/auth/register", json={
                "name": "Never Verified", "email": "stuck@acme.io",
                "password": "supersecret1", "mobile": "+919000000021", "acceptTerms": True,
            })

            async def age_signup():
                await database.users_collection().update_one(
                    {"email": "stuck@acme.io"},
                    {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(hours=30)}},
                )
            anyio.run(age_signup)

            outbox.clear()
            r = client.post("/notifications/verification-reminders", headers=ops)
            check("the verification reminder run reports what it did", r.status_code == 200 and "sent" in r.json()["data"], r.text)
            reminder = next((m for m in outbox if m["to"] == "stuck@acme.io"), {})
            check("a stuck account is chased", bool(reminder), str([m["to"] for m in outbox]))
            check("accounts that already verified are left alone", all(m["to"] == "stuck@acme.io" for m in outbox), str([m["to"] for m in outbox]))

            # The code from signup expired within minutes, so the reminder has
            # to mint a fresh one — resending a dead code is worse than
            # sending nothing.
            fresh_code = reminder.get("subject", "").split()[0]
            check("the reminder carries a six-digit code", fresh_code.isdigit() and len(fresh_code) == 6, reminder.get("subject", ""))
            v = client.post("/auth/verify-email/otp", json={"email": "stuck@acme.io", "code": fresh_code})
            check("and that fresh code actually works", v.status_code == 200, v.text)

            outbox.clear()
            client.post("/notifications/verification-reminders", headers=ops)
            check("nobody is chased twice", outbox == [], str([m["to"] for m in outbox]))
            r = client.post("/notifications/verification-reminders")
            check("the reminder run needs the ops secret -> 401", r.status_code == 401, r.text)
            r = client.post("/notifications/free-credit-reminders", headers=ops)
            check("the credit reminder run reports what it did", r.status_code == 200 and "sent" in r.json()["data"], r.text)
            r = client.post("/notifications/monthly-reports", headers=ops, json={})
            check("the monthly run names the month it covered", r.status_code == 200 and len(r.json()["month"]) == 7, r.text)
            check("accounts with no usage last month get no report", r.json()["data"]["sent"] == 0, r.text)
            r = client.post("/notifications/monthly-reports", json={})
            check("the monthly run needs the ops secret -> 401", r.status_code == 401, r.text)

            # This run's metering all landed in the current month, so asking for
            # that month is what actually builds a report from real records.
            outbox.clear()
            this_month = datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00Z")
            r = client.post("/notifications/monthly-reports", headers=ops, json={"month": this_month})
            check("accounts with usage do get a report", r.status_code == 200 and r.json()["data"]["sent"] >= 1, r.text)
            report_mail = next((m for m in outbox if m["subject"].startswith("Your ChatBucket usage report")), {})
            check("the report renders from real usage records", "{{" not in (report_mail.get("html") or "x"), str(report_mail.get("html"))[:200])
            check("the report states the reporting period", " - " in report_mail.get("subject", ""), report_mail.get("subject", ""))
            body = report_mail.get("body", "")
            check("the report lists the four headline metrics", body.count("vs ") == 4, body)
            check("the report names the plan", "Plan: " in body, body)
            check("the report ranks services by spend", "Top services by spend:" in body, body)

            r = client.post("/notifications/monthly-reports", headers=ops, json={"month": this_month})
            check("re-running a month mails nobody twice", r.json()["data"]["sent"] == 0, r.text)
        finally:
            os.environ.pop("OPS_SECRET", None)
            get_settings.cache_clear()

        # --- CORS ------------------------------------------------------------
        # The suite runs as `development`, so the loopback rule is live here.
        def preflight(origin):
            return client.options("/pricing", headers={
                "Origin": origin, "Access-Control-Request-Method": "GET",
            }).headers.get("access-control-allow-origin")

        check("a listed origin is allowed", preflight("http://localhost:3000") is not None, "")
        # `next dev` quietly picks 3001 when 3000 is busy; that used to be a
        # failed preflight with nothing said on the server side.
        check("in dev, any localhost port is allowed", preflight("http://localhost:3001") is not None, "")
        check("in dev, any loopback IP port is allowed", preflight("http://127.0.0.1:5173") is not None, "")
        check("an unknown site is still refused", preflight("https://evil.example.com") is None, "")
        check("a lookalike host is refused", preflight("https://chatbucket.business.evil.com") is None, "")

        # The loopback rule must never reach production, where it pairs with
        # allow_credentials and would hand any local page a customer session.
        from app.config import Settings  # noqa: PLC0415

        # A production Settings needs a real secret and an SMTP host, or the
        # config validator refuses to build it — which is itself the behaviour
        # asserted further up.
        production = dict(
            environment="production",
            jwt_secret="not-the-placeholder-" + "x" * 24,
            smtp_host="smtp.example.com",
        )
        dev_regex = Settings(environment="development").cors_origin_regex
        prod_regex = Settings(**production).cors_origin_regex
        check("the loopback rule exists in development", dev_regex is not None, str(dev_regex))
        check("and is absent in production", prod_regex is None, str(prod_regex))

        # --- the notification scheduler ------------------------------------
        from app import notifications  # noqa: PLC0415

        # Eligibility is a pure function of the clock, so it can be asserted
        # without waiting for one.
        def due(dt):
            return {job for job, _ in notifications._due_jobs(dt)}

        def period_for(dt, job):
            return next(p for j, p in notifications._due_jobs(dt) if j == job)

        check("nothing is due before the configured hour", due(datetime(2026, 8, 16, 5, 59)) == set(), str(due(datetime(2026, 8, 16, 5, 59))))
        check("the daily jobs are due after it", due(datetime(2026, 8, 16, 6, 0)) == {"free_credit_reminders", "onboarding_nudges", "verification_reminders"}, str(due(datetime(2026, 8, 16, 6, 0))))
        check("the monthly report is due on the 1st", "monthly_reports" in due(datetime(2026, 9, 1, 6, 0)), str(due(datetime(2026, 9, 1, 6, 0))))
        check("it reports the month that just ended", period_for(datetime(2026, 9, 1, 6, 0), "monthly_reports") == "2026-08", "")
        check("an outage on the 1st is caught up on the 3rd", "monthly_reports" in due(datetime(2026, 9, 3, 9, 0)), "")
        check("the catch-up run still covers the right month", period_for(datetime(2026, 9, 3, 9, 0), "monthly_reports") == "2026-08", "")
        # Bounded so that enabling the scheduler mid-month does not fire a
        # surprise retroactive send to the whole customer base.
        check("no retroactive monthly send past the catch-up window", "monthly_reports" not in due(datetime(2026, 9, 8, 9, 0)), "")
        check("the year boundary rolls correctly", period_for(datetime(2027, 1, 1, 7, 0), "monthly_reports") == "2026-12", "")
        check("February is handled", period_for(datetime(2027, 3, 1, 7, 0), "monthly_reports") == "2027-02", "")

        # The claim is what stops two workers doing the same run. Calling twice
        # stands in for two workers waking at the same moment.
        tick = datetime(2026, 8, 16, 6, 30)
        first = anyio.run(notifications.run_due_jobs, tick)
        second = anyio.run(notifications.run_due_jobs, tick)
        check("a due tick runs the daily jobs", set(first) == {"free_credit_reminders", "onboarding_nudges", "verification_reminders"}, str(first))
        check("a second worker on the same tick does nothing", second == {}, str(second))
        check("a tick before the hour does nothing", anyio.run(notifications.run_due_jobs, datetime(2026, 8, 16, 5, 0)) == {}, "")
        check("the next day is a new period and runs again", set(anyio.run(notifications.run_due_jobs, datetime(2026, 8, 17, 6, 30))) == {"free_credit_reminders", "onboarding_nudges", "verification_reminders"}, "")

        async def read_job_runs():
            return await database.job_runs_collection().find({}).to_list(length=None)

        runs = anyio.run(read_job_runs)
        check("each run is recorded once per period", len(runs) == len({(r["job"], r["period"]) for r in runs}), str([(r["job"], r["period"]) for r in runs]))
        check("a finished run records its outcome", all("finished_at" in r and "result" in r for r in runs), str(runs[:1]))

        # A failing job must hand its period back, or one bad tick would mean
        # nobody gets that month's report at all.
        original = notifications.send_onboarding_nudges

        async def explode():
            raise RuntimeError("simulated failure")

        notifications.send_onboarding_nudges = explode
        try:
            anyio.run(notifications.run_due_jobs, datetime(2026, 8, 18, 6, 30))
        finally:
            notifications.send_onboarding_nudges = original
        after_failure = anyio.run(read_job_runs)
        check(
            "a failed run releases its claim for retry",
            not any(r["job"] == "onboarding_nudges" and r["period"] == "2026-08-18" for r in after_failure),
            str([(r["job"], r["period"]) for r in after_failure]),
        )
        check("and the retry then succeeds", "onboarding_nudges" in anyio.run(notifications.run_due_jobs, datetime(2026, 8, 18, 6, 30)), "")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
