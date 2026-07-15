"""End-to-end smoke test for the B2B platform endpoints.

Drives the real FastAPI app against an in-memory async Mongo, exercising the
whole customer journey: register -> login -> profile -> API key -> meter usage
-> history/summary -> pricing -> password reset.

Run:  python -m scripts.smoke_b2b   (exits non-zero on any failure)
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app import database
from app.main import app

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
        services = {row["service"] for row in j["data"]}
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
            },
        )
        j = r.json()
        token = j.get("access_token", "")
        check("POST /auth/register 201 + token", r.status_code == 201 and bool(token), r.text)
        check("register response hides password_hash", "password_hash" not in j["user"], r.text)

        r = client.post(
            "/auth/register",
            json={"name": "Dup", "email": "ops@acme.io", "password": "supersecret1"},
        )
        check("register duplicate email -> 409", r.status_code == 409, r.text)

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

        r = client.put("/profile", headers=auth, json={"company": "Acme Global", "phone": "+91-99999"})
        check("PUT /profile updates company", r.json()["data"]["company"] == "Acme Global", r.text)

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

        # --- record usage (needs API key, not JWT) ---------------------
        r = client.post("/usage", json={"service": "stt_streaming", "quantity": 10})
        check("POST /usage without API key -> 401", r.status_code == 401, r.text)

        headers_key = {"X-API-Key": api_key}
        r = client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 10})
        check("POST /usage stt 10min cost 5.2", r.status_code == 201 and r.json()["data"]["cost"] == 5.2, r.text)
        client.post("/usage", headers=headers_key, json={"service": "tts_offline", "quantity": 2000})  # 0.78*2 = 1.56
        client.post("/usage", headers=headers_key, json={"service": "voip_call", "quantity": 4})       # 20.0
        client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 5})   # 2.6

        r = client.post("/usage", headers={"X-API-Key": "cb_live_bogus"}, json={"service": "stt_streaming", "quantity": 1})
        check("POST /usage bad API key -> 401", r.status_code == 401, r.text)

        # --- history + summary (JWT) -----------------------------------
        r = client.get("/usage", headers=auth)
        check("GET /usage history count = 4", r.json()["count"] == 4, r.text)
        r = client.get("/usage?service=stt_streaming", headers=auth)
        check("GET /usage filtered by service = 2", r.json()["count"] == 2, r.text)

        r = client.get("/usage/summary", headers=auth)
        j = r.json()
        # 5.2 + 1.56 + 20.0 + 2.6 = 29.36
        check("GET /usage/summary grand_total = 29.36", j["grand_total"] == 29.36, r.text)
        stt = next((s for s in j["by_service"] if s["service"] == "stt_streaming"), None)
        check("summary stt_streaming total 7.8 over 2 events", stt and stt["total_cost"] == 7.8 and stt["events"] == 2, str(stt))

        # --- revoke key blocks usage -----------------------------------
        r = client.delete(f"/api-keys/{key_id}", headers=auth)
        check("DELETE /api-keys/{id} revokes", r.status_code == 200, r.text)
        r = client.post("/usage", headers=headers_key, json={"service": "stt_streaming", "quantity": 1})
        check("POST /usage with revoked key -> 401", r.status_code == 401, r.text)

        # --- forgot / reset password -----------------------------------
        r = client.post("/auth/forgot-password", json={"email": "ops@acme.io"})
        reset_token = r.json().get("reset_token")  # exposed in dev
        check("POST /auth/forgot-password returns dev token", r.status_code == 200 and bool(reset_token), r.text)
        r = client.post("/auth/reset-password", json={"token": reset_token, "new_password": "finalsecret9"})
        check("POST /auth/reset-password ok", r.status_code == 200, r.text)
        r = client.post("/auth/login", json={"email": "ops@acme.io", "password": "finalsecret9"})
        check("login with reset password", r.status_code == 200, r.text)
        r = client.post("/auth/reset-password", json={"token": reset_token, "new_password": "another123"})
        check("reused reset token -> 400", r.status_code == 400, r.text)

        # --- forgot-password for unknown email does not leak -----------
        r = client.post("/auth/forgot-password", json={"email": "ghost@nowhere.io"})
        check("forgot-password unknown email still 200, no token", r.status_code == 200 and "reset_token" not in r.json(), r.text)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
