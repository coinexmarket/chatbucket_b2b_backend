# ChatBucket B2B Backend

Python (FastAPI + MongoDB) backend for the ChatBucket **B2B API platform**:
customer accounts, API keys, and **usage-based billing** for the AI services
(STT, TTS, translation, chat agent, voice/VoIP agents). It also serves the
`chatbucket-web` site endpoints (blogs, app-launch subscriptions, hackathon
contest).

MongoDB access is fully isolated in one dedicated layer (`app/database.py`),
with **separate databases**: `chatbucket_b2b` (accounts/keys/usage),
`chatbucket` (blog content + subscriptions), and `ChatBucketHackathon` (contest).

## Project layout

```
app/
  main.py            FastAPI app: lifespan, CORS, index creation, routers, health
  config.py          Env-driven settings (Mongo URIs, JWT, pricing currency, CORS)
  database.py        The ONLY module that talks to Mongo (Motor async client)
  security.py        Password hashing (PBKDF2), JWT tokens, API-key generation
  money.py           Exact INR amounts: Decimal -> Decimal128 -> float (JSON)
  pricing.py         Rate card + cost calculation (single source of truth)
  deps.py            Auth dependencies: get_current_user (JWT) / get_api_user (key)
  responses.py       Blog { status, status_code, message, data, ... } envelope
  serialization.py   ObjectId/date -> JSON; overview projection; public_user
  models/
    auth.py          Register / login / reset / profile / api-key bodies
    usage.py         Usage-metering body
    requests.py      Subscription + contest bodies
  routers/
    auth.py          register, login, forgot-password, reset-password
    profile.py       get/update profile, change password
    api_keys.py      create (once-shown), list (masked), revoke
    usage.py         record usage, estimate, history, summary
    pricing.py       public rate card
    blogs.py / subscriptions.py / contest.py   (chatbucket-web site)
scripts/
  seed.py            Sample blog data
  smoke_test.py      End-to-end test for the site endpoints (22 checks)
  smoke_b2b.py       End-to-end test for the B2B platform (34 checks)
```

## Billing model (usage-based, INR)

`cost = rate × quantity ÷ per`

| Service key | Unit | Rate | Per |
| --- | --- | --- | --- |
| `stt_streaming` | minutes | ₹0.52 | 1 min |
| `stt_offline` | minutes | ₹0.39 | 1 min |
| `tts_streaming` | characters | ₹0.91 | 1000 chars |
| `tts_offline` | characters | ₹0.78 | 1000 chars |
| `translation` | tokens | ₹7.50 | 10,000 tokens |
| `chat_agent` | tokens | ₹4.38 | 10,000 tokens |
| `voice_agent_web` | minutes | ₹4.00 | 1 min |
| `voip_call` | minutes | ₹5.00 | 1 min |

Rates live in `app/pricing.py`. Example: 2,500 chars of `tts_streaming`
= `0.91 × 2500 / 1000` = **₹2.275**.

Amounts are **exact decimals**, never binary floats: rates are `Decimal`, costs
are stored as BSON `Decimal128` (which Mongo's `$sum` adds exactly), and the
conversion to a JSON number happens only at the response boundary. `money.py`
owns those edges — nothing else should call `float()` on an amount. Responses
are unchanged: `cost` is still a plain JSON number like `2.275`.

## B2B API

### Auth  `/auth`
| Method | Path | Auth | Body → Result |
| --- | --- | --- | --- |
| POST | `/auth/register` | – | `{name,email,password,company?}` → `{access_token, user}` (201) |
| POST | `/auth/login` | – | `{email,password}` → `{access_token, token_type, expires_in, user}` |
| POST | `/auth/forgot-password` | – | `{email}` → always 200 (returns `reset_token` only in dev) |
| POST | `/auth/reset-password` | – | `{token,new_password}` → 200 / 400 if expired |

### Profile  `/profile`  (Bearer JWT)
| Method | Path | Body |
| --- | --- | --- |
| GET | `/profile` | – |
| PUT | `/profile` | `{name?,company?,phone?}` |
| PUT | `/profile/password` | `{current_password,new_password}` → signs out other sessions, returns a fresh `access_token` for the caller |

### API keys  `/api-keys`  (Bearer JWT)
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api-keys` | `{name}` → returns plaintext `api_key` **once** (`cb_live_…`) |
| GET | `/api-keys` | List, masked as `cb_live_****ABCD` |
| DELETE | `/api-keys/{id}` | Revoke |

### Usage & billing  `/usage`, `/pricing`
| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/pricing` | – | The rate card. |
| POST | `/usage/estimate` | – | `{service,quantity}` → cost, nothing stored. |
| POST | `/usage` | **X-API-Key** | Record consumption; computes + stores INR cost (201). Send `Idempotency-Key` to make retries safe — a replay returns the original record with 200. |
| GET | `/usage` | Bearer JWT | History; `?service=&limit=`. |
| GET | `/usage/summary` | Bearer JWT | Per-service totals + `grand_total`. |

`POST /usage` is machine-to-machine: your STT/TTS/translation/voice services
call it with the customer's API key after each unit of work, e.g.

```bash
curl -X POST http://localhost:8000/usage \
  -H "X-API-Key: cb_live_xxx" -H "Content-Type: application/json" \
  -H "Idempotency-Key: 9f3c-session-abc-chunk-7" \
  -d '{"service":"stt_streaming","quantity":12.5,"metadata":{"session":"abc"}}'
```

**Always send `Idempotency-Key`** from those services: a unique value per usage
event (a session/chunk id works well). Without it, a retry after a timeout
bills the customer twice, silently and irreversibly. With it, a retry returns
the record the first call stored — `{"replayed": true, ...}` with HTTP 200 —
and the customer is charged once. Keys are scoped per customer, so two
customers may use the same value.

## chatbucket-web site endpoints

Blog envelope: `{ status, status_code, message, data, response_code }`.

| Method | Path |
| --- | --- |
| GET | `/v1/blogs`, `/v1/blogs/{slug}`, `/v2/blogs/{slug}?category=&sub_category=` |
| GET | `/v1/recent-blogs`, `/v1/related-blogs/{category}`, `/v1/featured-blogs` |
| GET | `/v1/categories`, `/v1/c-blogs?categories=a,b&text=q` |
| POST | `/subscriptions/v1/notify-app-launch` `{email}` (409 `{err_code,error}` on dup) |
| POST | `/api/register` · GET `/api/verify?id=CB-HACK-2026-XXXXXX` |

### Health
`GET /` (liveness) and `GET /health` — checks DB connectivity and returns
**503** when Mongo is unreachable, so load balancers drop the instance. Its
`indexes` field reads `ready` once every index exists; a lasting `pending`
means the background retry is stuck and needs investigating.

## Local development

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then set MONGODB_URI and a real JWT_SECRET

python -m scripts.seed      # optional: sample blogs/categories

uvicorn app.main:app --reload --port 8000
```

Interactive docs: <http://localhost:8000/docs>

### Tests (no MongoDB required — in-memory)

```bash
pip install mongomock-motor httpx
python -m scripts.smoke_b2b     # 34 checks: accounts, keys, usage, billing
python -m scripts.smoke_test    # 22 checks: blogs, subscriptions, contest
```

## Docker

```bash
docker build -t chatbucket-b2b-backend .
docker run -p 8000:8000 --env-file .env chatbucket-b2b-backend
```

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | `mongodb://localhost:27017` | Connection string (required in prod). |
| `B2B_DB_NAME` | `chatbucket_b2b` | Accounts, API keys, usage. |
| `BLOG_DB_NAME` | `chatbucket` | Blogs, categories, subscriptions. |
| `CONTEST_DB_NAME` | `ChatBucketHackathon` | Contest registrations. |
| `JWT_SECRET` | `dev-insecure-change-me` | **Change in production.** The app refuses to start if this is still the default and `ENVIRONMENT` is not a dev value. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | JWT lifetime. |
| `RESET_TOKEN_EXPIRE_MINUTES` | `30` | Password-reset token lifetime. |
| `CURRENCY` | `INR` | Billing currency label. |
| `CORS_ORIGINS` | localhost + chatbucket domains | Allowed browser origins. |
| `ENVIRONMENT` | `development` | In `development`, `forgot-password` returns the reset token. **Set this to `production` when deploying** — the default is a dev value, so leaving it unset both exposes reset tokens and skips the `JWT_SECRET` check. |

## Notes

* **Password hashing** uses stdlib PBKDF2-HMAC-SHA256 (salted, 240k iterations) —
  no native/Rust build dependency, which matters on very new Python versions.
* **Account enumeration**: `login` verifies against a throwaway hash when no
  user matches, so it takes the same time whether or not the email exists;
  `forgot-password` always returns the same response. Note `register` still
  answers 409 on a known email — inherent without an email-verification flow.
* **Indexes** are created at startup. A failure there does not crash the app;
  it retries in the background with backoff, and `register` falls back to an
  explicit duplicate check until the unique email index is confirmed present.
* **Forgot-password** currently returns the reset token in development. Wire an
  email provider in `routers/auth.py` (`TODO`) to send a reset link in prod.
* **Token revocation**: users carry a `token_version`, echoed in each JWT's
  `ver` claim and checked on every authenticated request. Resetting or changing
  a password increments it, so tokens issued earlier stop working immediately
  rather than staying valid for the rest of their 24h lifetime. API keys are a
  separate credential and are unaffected — revoke them via `/api-keys/{id}`.
* The blog/subscription/contest modules are the `chatbucket-web` site API; if
  this repo should be B2B-only, they can be dropped without touching the
  platform code.
