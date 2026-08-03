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
  sessions.py        Refresh tokens: rotation + reuse detection
  ratelimit.py       Mongo-backed fixed-window request limits
  email.py           The ONLY module that sends mail (SMTP + dev/test backends)
  money.py           Exact INR amounts: Decimal -> Decimal128 -> float (JSON)
  pricing.py         Rate card + cost calculation (single source of truth)
  analytics.py       Chart time-bucketing: granularities, ranges, zero-fill
  status.py          Service status registry, staleness, daily rollups
  plans.py           Plan catalogue: rate limits + top-up packs
  credits.py         Credit balances + append-only ledger (atomic spend guard)
  deps.py            Auth dependencies: get_current_user (JWT) / get_api_user (key)
  responses.py       Blog { status, status_code, message, data, ... } envelope
  serialization.py   ObjectId/date -> JSON; overview projection; public_user
  models/
    auth.py          Register / login / reset / profile / api-key bodies
    usage.py         Usage-metering body
    billing.py       Top-up / auto-recharge / webhook bodies
    projects.py      Project create / update bodies
    requests.py      Subscription + contest + demo-request bodies
  routers/
    auth.py          register, login, forgot-password, reset-password
    profile.py       get/update profile, change password
    api_keys.py      create (once-shown), list (masked), revoke
    usage.py         record usage, estimate, history, summary
    pricing.py       public rate card
    limits.py        plan limits (dashboard Limits page)
    projects.py      projects CRUD (Create API Key modal)
    status.py        service status (API Status page)
    billing.py       credits, top-ups, ledger, gateway webhook
    demo.py          demo requests (Personal / Business modal)
    blogs.py / subscriptions.py / contest.py   (chatbucket-web site)
scripts/
  seed.py            Sample blog data
  smoke_test.py      End-to-end test for the site endpoints (61 checks)
  set_status.py      Set every system's status by hand
  smoke_b2b.py       End-to-end test for the B2B platform (300 checks)
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
| POST | `/auth/register` | – | `{name,email,password,mobile,accept_terms,company?,how_did_you_hear?}` → `{access_token, user}` (201) |
| POST | `/auth/login` | – | `{email,password}` → `{access_token, token_type, expires_in, user}` |
| POST | `/auth/forgot-password` | – | `{email}` → always 200 (returns `reset_token` only in dev) |
| POST | `/auth/reset-password` | – | `{token,new_password}` → 200 / 400 if expired |
| POST | `/auth/refresh` | – | `{refresh_token}` → a new access token; the refresh token is **rotated** |
| POST | `/auth/logout` | Bearer JWT | `{refresh_token?, all_sessions?}` → revoke one session or all |
| POST | `/auth/verify-email` | – | `{token}` → confirm the address (single use) |
| POST | `/auth/verify-email/resend` | Bearer JWT | Send a fresh link to the signed-in address |

#### Signup form fields

Backs the "Create Account" modal. Keys are accepted in `camelCase` or
`snake_case`, and **unknown fields are rejected with 422** rather than dropped —
without that, a form posting a field the API doesn't model gets a 201 and loses
it silently.

| Field | Required | Notes |
| --- | --- | --- |
| `name`, `email`, `password` | yes | Password minimum 8 characters. |
| `mobile` | yes | International format. `"+91 98765-43210"` is normalised and stored as `+919876543210`; a number with no country code is rejected. |
| `accept_terms` | yes | Must be `true`. Stored as `terms_accepted_at` + `terms_version` — a boolean alone can't say *when* or *to what*. |
| `how_did_you_hear` | no | Free text (≤200). Deliberately not an enum, so the form owns the option list. |
| `company` | no | |

Validation errors name the offending field in the casing it was sent, e.g.
`{"detail":[{"loc":["body","acceptTerms"],"msg":"Value error, You must accept
the Terms & Conditions to create an account."}]}`.

The mobile number is stored as **`phone`**, the field the user document and
`PUT /profile` already use, so "Mobile Number" doesn't become a second column
meaning the same thing. `PUT /profile` applies the same E.164 rule.

### Account  (Bearer JWT)
| Method | Path | Notes |
| --- | --- | --- |
| GET | `/account/export` | Everything held about the account, as JSON |
| POST | `/account/delete` | `{password}` → close the account |

**Deletion anonymises rather than erases.** Invoices, payments, usage and the
credit ledger are financial records most jurisdictions require kept for years,
so they stay — with the personal data stripped from the user document they
point at. Name, email, phone, company and billing details are cleared, every
API key is revoked and every session ended; the freed email address becomes
reusable by a genuine future signup.

`POST /account/delete`, not `DELETE /account`: the password confirmation needs a
request body, and RFC 9110 gives DELETE bodies no defined semantics — several
HTTP clients refuse to send one and intermediaries may drop it. A confirmation
that vanishes in transit is worse than an unfashionable verb.

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
| GET | `/api-keys` | List, masked as `cb_live_****ABCD`; `?limit=&offset=&include_revoked=` |
| PATCH | `/api-keys/{id}` | `{name}` → rename the label; the secret is unchanged |
| DELETE | `/api-keys/{id}` | Revoke |

### Usage & billing  `/usage`, `/pricing`
| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/pricing` | – | The rate card. |
| POST | `/usage/estimate` | – | `{service,quantity,model?}` (or `input_quantity`+`output_quantity`) → cost, nothing stored. |
| POST | `/usage` | **X-API-Key** | Record consumption; computes + stores INR cost and **debits credits** (201). Optional `model` attributes it to the model that served it. **402** when credits are exhausted — the usage is still recorded, with `billed:false`. Send `Idempotency-Key` to make retries safe: a replay returns the original record with 200 and is not charged again. |
| GET | `/usage` | Bearer JWT | History; `?service=&model=&api_key_id=&project_id=&limit=`. |
| GET | `/usage/summary` | Bearer JWT | `by_service`, `by_model`, `by_api_key`, `by_project` breakdowns + `grand_total`, `billed_total`, `unbilled_total`, `unattributed_cost`. |
| GET | `/usage/timeseries` | Bearer JWT | Charts; `?granularity=daily\|hourly\|minute&from=&to=` plus the filters above. |
| GET | `/usage/overview` | Bearer JWT | Headline totals vs the preceding period; `?days=` (1-365). |
| GET | `/usage/export.csv` | Bearer JWT | Usage as CSV, streamed; same filters plus `?from=&to=`. |

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

### Per-model usage

`POST /usage` takes an optional `model` — the model that actually served the
request ("Bulbul v3", "Sarvam 30b") — and `GET /usage/summary` returns a
`by_model` breakdown with cost, quantity, events and `share_percent`, which is
what the dashboard's model table shows.

```bash
-d '{"service":"tts_streaming","quantity":2500,"model":"Bulbul v3"}'
```

It is a **free string, not an enum**: models are added and renamed far more
often than this service is redeployed, so the caller owns the list.

### Input/output split pricing

Generating a token costs far more than reading one, and every major LLM API
prices them separately. A service can do the same — set **both** `input_rate`
and `output_rate` on it in `app/pricing.py`:

```python
Service("chat_agent", "Chat Agent", "tokens", Decimal("4.38"), 10000,
        input_rate=Decimal("1.50"), output_rate=Decimal("7.50")),
```

Callers then report the two sides instead of a total:

```bash
-d '{"service":"chat_agent","inputQuantity":8000,"outputQuantity":2000}'
#  1.50 x 8000/10000  +  7.50 x 2000/10000  =  Rs.2.70
```

`quantity` is **derived** from the split, so the usage breakdowns, charts and
CSV keep working unchanged. The record stores `input_rate`/`output_rate` and
both quantities, so history stays auditable after a price change.

**Both rates or neither** — a half-configured service would have to be priced by
guessing the other side. Same rule on the request: `quantity` **or**
`inputQuantity` + `outputQuantity`, never one side alone and never both forms
(422). Sending a split to a flat-priced service is a 400 rather than a silent
fallback to the blended rate.

A **model override always wins over the service**, in both directions: a
flat-rate `ModelRate` on a split-priced service makes that model flat. The more
specific price is the one that applies.

**It ships unset**, so `translation` and `chat_agent` still bill their single
blended rate and nothing changes until you fill in real numbers. Worth doing
before you have live data — a blended rate silently varies your margin with
each customer's output/input ratio.

#### Per-model prices

A model can cost more than its service's base rate — a 30B chat model need not
price like a small one. `MODEL_RATES` in `app/pricing.py` holds the overrides,
keyed by `(service, model)`:

```python
MODEL_RATES = {... for m in [
    ModelRate("chat_agent", "Sarvam 30b", Decimal("9.00")),
    ModelRate("chat_agent", "Tiny Model", Decimal("2.50"), unit_size=1000),
]}
```

`unit_size` is optional and defaults to the service's, so a model quoted per
1,000 tokens can sit alongside a service quoted per 10,000.

**It ships empty**, so out of the box every model bills at its service rate,
exactly as before — no prices were invented. Fill it in and `POST /usage`,
`POST /usage/estimate` and `GET /pricing` all use it immediately.

A model with **no entry falls back to the service rate rather than erroring**:
callers send arbitrary model names, and an unrecognised one must not fail a
billing call. Lookup is case- and space-insensitive, like the grouping.

Each usage record stores the `rate` and `unit_size` **actually charged**, so
history and replays stay correct after a price change.

To stop a typo becoming a permanent extra row, each record stores both the
name as sent (`model`, for display) and a normalised `model_key` (lower-cased,
whitespace collapsed) that all grouping and the `?model=` filter use. So
"Bulbul v3", "bulbul  V3" and " Bulbul V3 " are one row, while genuinely
different models can never be merged. Whitespace-only is stored as absent.

Records whose caller sent no model are **not** dropped from the maths: their
cost is reported as `unattributed_cost`, so `by_model` plus that figure
reconciles to `grand_total`. A non-zero value means some service is metering
without sending `model`.

### Usage analytics

`GET /usage/timeseries` buckets spend, requests and quantity over time for the
dashboard charts. `?granularity=` is `daily`, `hourly` or `minute`; `?from=` and
`?to=` accept a plain date (`2026-04-12`) or a full ISO timestamp, and all the
usual filters (`service`, `model`, `api_key_id`) apply.

**Empty buckets are returned as zeroes, not omitted** — dropping them would make
a quiet day vanish from the x-axis instead of showing as a flat line.

**Ranges are capped per granularity** (minute ≤ 2 days, hourly ≤ 62 days, daily
≤ 731) and exceed → 400 with what to do about it. A year at minute granularity
is 525,600 buckets; the API refuses rather than building that response.

Bucketing is **UTC**, using the same `$dateToString` format on both sides so the
generated and aggregated keys line up. Re-bucketing into a viewer's local zone
would let two people disagree about the same invoice.

`GET /usage/overview` gives the headline figures against the **immediately
preceding period of equal length** — `?days=30` compares with the 30 days
before that, not a calendar month. `change_percent` is **null**, not `0`, when
the previous period had no usage: both `0%` and `100%` would be inventing a
trend out of no baseline.

`GET /usage/summary` also returns `by_api_key`, labelled with each key's name
and masked value so a key filter can show "Production" rather than a raw id.
Usage from a key that no longer exists is labelled `(deleted key)` rather than
rendering blank.

## Sessions & rate limiting

### Refresh tokens

`/auth/login` and `/auth/register` return a `refresh_token` alongside the 24h
access token. `POST /auth/refresh` exchanges it for a fresh access token so a
session survives without keeping a long-lived credential in the browser.

Refresh tokens are **opaque and stored hashed**, not JWTs: a refresh token must
be revocable the instant someone signs out, which means checking it against
storage on every use — exactly what a self-contained JWT avoids.

**Rotation with reuse detection.** Each refresh spends the token and issues a
new one, so a token has exactly one valid use. Presenting a spent token means
the value leaked and two parties hold it; that cannot be told apart from the
attacker refreshing first, so the whole family descended from that sign-in is
revoked and everyone is signed out. Losing a session beats silently sharing it.

`POST /auth/logout` revokes one session, or all of them with
`{"all_sessions": true}` — which also bumps `token_version`, retiring live
access tokens immediately. A password change or reset revokes every refresh
token too; without that, `token_version` would retire access tokens while a
stolen refresh token quietly minted new ones.

### Rate limiting

Public endpoints are limited by a fixed window per (scope, identifier), counted
in **Mongo** rather than process memory — an in-process counter would give a
caller N times the limit on an N-worker deployment, which is no limit at all.
Expired windows are removed by a TTL index. Redis would be cheaper per hit;
`ratelimit.hit()` is the only thing that would change.

| Endpoint | Per IP | Per account |
| --- | --- | --- |
| `/auth/login` | 50 / 15 min | **5 / 15 min** |
| `/auth/register` | 10 / hour | – |
| `/auth/forgot-password` | 20 / hour | **3 / hour** |
| `/demo-requests` | 10 / hour | – |
| `/api/register` | 10 / hour | – |

The metered `POST /usage` is limited separately, by the caller's **plan** — see
[Plan rate limits](#plan-rate-limits-are-enforced).

Per-IP limits are loose and per-account limits tight **on purpose**: an office
shares one address behind NAT, so a strict per-IP cap locks out the colleagues
of whoever is being attacked. The per-account limit does the security work.
`forgot-password` is limited *before* the account lookup, so the limit is the
same whether or not the address is registered — a limit that only bit real
accounts would itself leak which ones exist.

Exceeding a limit returns **429** with `Retry-After`. The limiter **fails open**:
if the counter store is unreachable the request proceeds and the failure is
logged, because a limiter outage should not take sign-in down with it.

`X-Forwarded-For` is only believed when `TRUST_PROXY_HEADERS=true`. The header
is trivially forged, so trusting it by default would let anyone bypass every
per-IP limit by inventing an address per request.

## Projects

Backs the **Select Project** field on the Create API Key modal.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/projects` | `{name, description?}` — names unique per customer |
| GET | `/projects` | Paged, each with its `api_key_count` |
| GET | `/projects/{id}` | One project |
| PATCH | `/projects/{id}` | Rename / re-describe |
| DELETE | `/projects/{id}` | Delete, detaching its keys |

A project attaches to an **API key**, and **usage inherits the project of the
key that reported it**. The metering services therefore never need to know
projects exist, and attribution cannot drift from whichever credential actually
did the work. `POST /api-keys` and `PATCH /api-keys/{id}` take `project_id`
(`""` on the PATCH unassigns); both validate it belongs to the caller, so a
guessed id cannot attach a key to another customer's project.

Deleting a project **detaches its keys but leaves them working** — a project is
a label, not a credential, so removing one must not silently break an
integration. Historical usage keeps the project id it was recorded under, since
rewriting it would change what a past period cost; `by_project` labels those
rows `(deleted project)`.

Filter with `?project_id=` on `/usage`, `/usage/timeseries` and
`/usage/export.csv`.

## CSV export

`GET /usage/export.csv` streams usage records for spreadsheets and
reconciliation — streamed rather than assembled in memory, because a year of
metering is far more rows than should be held as one string before the first
byte goes out. Accepts every usage filter plus `?from=`/`?to=`. Amounts are
rendered through `money`, so the file carries the exact stored decimal rather
than a float that has been through a JSON round trip.

## Credits, plans & billing

Backs the dashboard's **Billing** and **Limits** pages. The product is
**prepaid credits, not subscriptions** — "add credits anytime, credits never
expire". **1 credit = ₹1**, so ₹10,000 buying 11,000 credits is a 10% bonus.

| Plan | Price | Credits | Rate limit | Concurrency |
| --- | --- | --- | --- | --- |
| `starter` | – (default) | – | 60 req/min | 2 |
| `pro` | ₹10,000 | 11,000 | 200 req/min | 10 |
| `business` | ₹50,000 | 57,500 | 1,000 req/min | 50 |

Buying a pack grants its credits *and* moves the account onto that tier.
Plans live in `app/plans.py`, the way rates live in `app/pricing.py`.

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/limits/plans` | – | Plan catalogue. |
| GET | `/limits` | Bearer JWT | Caller's plan, credits, per-service limits. |
| GET | `/billing` | Bearer JWT | Balance + auto-recharge settings. |
| GET | `/billing/history` | Bearer JWT | Credit ledger; `?kind=&limit=`. |
| GET | `/billing/payments` | Bearer JWT | Top-up orders and their status. |
| GET | `/billing/details` | Bearer JWT | Invoicing identity (legal name, GSTIN, address). |
| PUT | `/billing/details` | Bearer JWT | Set it. Never alters an invoice already issued. |
| GET | `/billing/invoices` | Bearer JWT | Issued invoices, newest first. |
| GET | `/billing/invoices/{id}` | Bearer JWT | One invoice, by id **or** by number (`INV-0001`). |
| PUT | `/billing/auto-recharge` | Bearer JWT | `{enabled, threshold_credits?, amount_inr?}` |
| POST | `/billing/top-up` | Bearer JWT | `{plan}` **or** `{amount_inr}` → a *pending* payment (201). |
| POST | `/billing/payments/{id}/verify` | Bearer JWT | Razorpay Checkout callback — signature-verified, then settles. |
| POST | `/billing/webhook/razorpay` | `X-Razorpay-Signature` | Razorpay webhook — the authoritative confirmation. |
| POST | `/billing/payments/{id}/confirm` | `X-Billing-Secret` | Manual/no-gateway settlement: mark paid, grant credits, **issue the invoice**. |

### How credits are spent

`POST /usage` now debits the owner's balance and returns **402** when it will
not cover the call. The debit happens *after* the usage insert, so the
idempotency index has already rejected any replay — a retried call is never
charged twice.

Consumption that could not be paid for is **still recorded**, with
`billed: false`. The work already happened; dropping the record would only
hide it. The 402 tells the calling service to stop serving that customer.
`GET /usage/summary` therefore reports three figures: `grand_total` (everything
consumed), `unbilled_total` (of which never charged) and `billed_total`.

The balance lives in `credit_accounts` as **integer minor units**, not
`Decimal128`, because the overspend guard is a single conditional update
(`{$gte: n}` + `{$inc: -n}`) and that has to be atomic. Read-then-write would
let ten concurrent calls each pass a check only three can afford — the smoke
test asserts exactly this, and drives the naive version to −200 credits.
`credit_ledger` is the append-only history the billing table lists.

### Granting credits

Only `POST /billing/payments/{id}/confirm` grants credits, and it requires
`BILLING_WEBHOOK_SECRET` (compared in constant time). There is deliberately no
authenticated route by which a customer can credit their own account. With the
secret unset the endpoint returns **503** — an unconfigured gateway must not
mean "anyone may grant credits". Webhook redelivery is safe: the order is
claimed with a conditional update, so a replay reports `replayed: true` and
credits nothing.

### Invoices

Confirming a payment issues exactly one invoice, returned on the confirm
response and listed at `GET /billing/invoices`. The number appears on the
payment too, which is what the billing table's **Invoice** column shows.

**Numbers are gap-free.** They come from an atomic `$inc` on a counter
document, not a count of existing invoices — counting would hand the same
number to two concurrent payments. Verified: 20 concurrent draws produce 20
unique, gapless numbers. Format is `INV-0001` (`INVOICE_NUMBER_PREFIX` /
`INVOICE_NUMBER_PADDING`); the sequence is global, so change the prefix only
between accounting periods.

**Invoices are immutable.** The customer's billing details are *snapshotted
onto* each one. Referencing the profile instead would silently rewrite last
year's invoices whenever someone moves office — precisely what an invoice
exists to prevent. Editing `/billing/details` is asserted not to change an
already-issued invoice.

Issuance is the last step of confirmation and cannot fail it: the money has
already moved, so a numbering error is logged and the payment still succeeds.
A missing invoice is recoverable; a failed confirmation is not. Webhook
redelivery never mints a second one — issuance sits inside the conditional
claim, and `payment_id` is uniquely indexed as a backstop.

`bill_to.complete` is false when the customer never filled the billing form
in, so finance can chase the incomplete ones rather than discover them later.

> **Tax is NOT computed.** Each invoice records `tax_status: "not_computed"`
> rather than a zero, because a zero asserts "no tax applies" — a claim this
> service is in no position to make. GST treatment (rate, CGST/SGST vs IGST by
> place of supply, reverse charge on exports) is a compliance decision, not a
> default. Until it is settled, either treat these as internal receipts, or set
> `provider_invoice_id`/`provider_invoice_url` on the webhook and let the
> gateway issue the GST-compliant document — those references are stored and
> returned so the dashboard can link to the authoritative one.

### Razorpay

Set `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` and `POST /billing/top-up` also
creates a Razorpay order, returning a `checkout` block with everything the
browser needs (`key_id`, `order_id`, `amount` in paise). Leave them unset and
the endpoint behaves exactly as before — a local pending payment settled by the
shared-secret endpoint.

**Two verified paths settle a payment, and both converge on one function.**

| Path | Verified by |
| --- | --- |
| `POST /billing/payments/{id}/verify` — Checkout callback | `HMAC_SHA256(order_id\|payment_id, KEY_SECRET)` |
| `POST /billing/webhook/razorpay` — webhook | `HMAC_SHA256(raw_body, WEBHOOK_SECRET)` |

> **`RAZORPAY_WEBHOOK_SECRET` is not the key secret.** It is a separate value
> set when registering the webhook in the dashboard. Signing with the wrong one
> rejects every delivery — the suite asserts a webhook signed with the key
> secret is refused, precisely because mixing them is the usual mistake.

The **webhook is authoritative**: a customer can close the browser before the
callback fires, and Razorpay still delivers. The callback exists so the
dashboard can show credits immediately rather than waiting. Whichever arrives
first wins — settlement claims the order with a conditional update, so the
other reports `replayed: true` and credits nothing.

The callback arrives through the customer's browser and is therefore untrusted:
the order id must match the one *this* payment was created with, or a valid
signature from a different order could settle it. Both are asserted.

`provider_payment_id` is uniquely indexed, so one gateway payment cannot settle
two orders — that returns **409** rather than crediting twice.

Amounts are rounded to whole paise before the order is created: a gateway can
only charge integer minor units, and the money taken must equal the money
recorded. `money.to_paise` raises rather than rounding silently.

If Razorpay is unreachable, `/billing/top-up` returns **502** and leaves a
pending record to retry — never a charge with nothing pointing at it.

### What is *not* implemented

* **Plan concurrency is reported, not enforced.** Each plan advertises a
  `concurrency` figure and `GET /limits` returns it, but nothing counts
  in-flight requests against it. Only the per-minute rate is enforced (below).
* **Auto-recharge settings are stored but never trigger.** Charging a customer
  unattended needs a saved payment method held by the gateway. The response
  says so explicitly rather than implying a top-up will happen.
* **Tax is not computed on invoices** — see **Invoices** above. Until the GST
  treatment is settled these are internal receipts, not tax invoices.

### Plan rate limits *are* enforced

`POST /usage` applies the caller's plan allowance (`requests_per_minute`) and
returns **429** with `Retry-After` past it; every response carries
`X-RateLimit-Limit` / `-Remaining` / `-Reset`. `GET /limits` reports whether
this is on via `enforced`, which follows `ENFORCE_PLAN_RATE_LIMITS` (default
**true**) — set it to `false` to meter without blocking during a migration.

The counter is the same **Mongo-backed** limiter the public endpoints use, so
the allowance holds across workers; an in-process one would give a customer N×
the limit on an N-worker deployment.

The window is scoped to **(account, service)**, not to the API key — every key
on an account shares one allowance, so minting extra keys cannot buy extra
throughput — and each service is limited independently, so a burst of STT does
not throttle a customer's chat traffic.

> **Deploying this to an existing install:** every current account has a zero
> balance, so with `ENFORCE_CREDIT_BALANCE=true` (the default) their metered
> calls start returning 402 immediately. Either top them up first, or set
> `ENFORCE_CREDIT_BALANCE=false` to meter without blocking while you migrate.

## chatbucket-web site endpoints

Blog envelope: `{ status, status_code, message, data, response_code }`.

| Method | Path |
| --- | --- |
| GET | `/v1/blogs`, `/v1/blogs/{slug}`, `/v2/blogs/{slug}?category=&sub_category=` |
| GET | `/v1/recent-blogs`, `/v1/related-blogs/{category}`, `/v1/featured-blogs` |
| GET | `/v1/categories`, `/v1/c-blogs?categories=a,b&text=q` |
| POST | `/subscriptions/v1/notify-app-launch` `{email}` (409 `{err_code,error}` on dup) |
| POST | `/api/register` · GET `/api/verify?id=CB-HACK-2026-XXXXXX` |
| POST | `/demo-requests` — see below |

### Demo requests  `POST /demo-requests`

Backs the "Let's get your demo started" modal. One endpoint serves both tabs;
the body is a **discriminated union on `type`**, so the Business tab's
`company_name` is required without forcing it on Personal leads. Fields are
accepted in either `camelCase` (what the React form holds) or `snake_case`, and
always stored as `snake_case`.

| Field | personal | business |
| --- | --- | --- |
| `type` | `"personal"` | `"business"` |
| `name`, `email`, `mobile` | required | required |
| `subscribe_updates` | optional, default `false` | optional, default `false` |
| `how_did_you_hear` | optional | – |
| `company_name` | – | **required** |
| `company_details` | – | optional |

```bash
curl -X POST http://localhost:8000/demo-requests \
  -H "Content-Type: application/json" \
  -d '{"type":"business","name":"Alan Turing","email":"alan@acme.com",
       "mobile":"9876500000","companyName":"Acme Global"}'
```

Returns 201 `{status, message, data:{id, type, created_at}}`. Leads land in
`demo_requests` (B2B database) with `status: "new"`. **Duplicates are accepted
on purpose** — a repeat request is a real lead, so de-duplication belongs in the
CRM rather than in a 409 that discards it.

Each lead is emailed to `SALES_EMAIL` (see **Email** below); leave that unset and
the request is still recorded, just not announced. The endpoint is public and
unauthenticated but rate limited per IP (10/hour), as is `/api/register`; there
is no captcha, so a determined submitter can still fill the CRM with noise.

## Email

`app/email.py` is the only module that sends mail, the way `database.py` is the
only one that touches Mongo. Transport is **SMTP**, which SendGrid, SES,
Mailgun, Postmark and Gmail all speak — so changing provider is configuration,
not code, and needs no extra dependency.

| `EMAIL_BACKEND` | Behaviour |
| --- | --- |
| `auto` *(default)* | SMTP when `SMTP_HOST` is set, otherwise `console`. |
| `smtp` | Really send. Requires `SMTP_HOST`. |
| `console` | Log the message instead of sending (local development). |
| `memory` | Append to `email.outbox` (used by the smoke tests). |
| `disabled` | Drop silently. |

Two messages are sent today: the **password-reset link** and the **demo-request
notification** to sales.

Both are dispatched as FastAPI background tasks, after the response. That is not
just for latency — `forgot-password` returns an identical response whether or
not the email exists, and awaiting an SMTP round trip only for real accounts
would leak the difference in the response *time*, reinstating the enumeration
oracle the endpoint is written to avoid.

**Sending never fails a request.** A mail outage is logged and returned as
`False`; it does not turn a successful password reset or a captured lead into a
500. The trade-off is that a lost email is only visible in the logs — grep for
`chatbucket_b2b.email`. The backend in use is logged once at startup.

In production, `EMAIL_BACKEND=auto` with no `SMTP_HOST` **refuses to start**:
otherwise reset links would be written to the log and silently never delivered,
which looks exactly like a working password reset. Set `EMAIL_BACKEND=disabled`
to state that a deployment intentionally sends none.

Email bodies are deliberately ASCII-only: a non-ASCII character makes Python
pick `8bit` transfer encoding, which SMTP servers that do not advertise
`8BITMIME` may reject.

### Health
`GET /` (liveness) and `GET /health` — checks DB connectivity and returns
**503** when Mongo is unreachable, so load balancers drop the instance. Its
`indexes` field reads `ready` once every index exists; a lasting `pending`
means the background retry is stuck and needs investigating.

## Service status

Backs the **API Status** page. `GET /status` is public — a status page nobody
can read during an outage is not much of a status page — and returns all six
systems with 90 days of daily history for the uptime strip.

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/status` | – |
| GET | `/status/{service}` | – |
| POST | `/status/heartbeat` | `X-Status-Secret` |
| PUT | `/status/{service}` | `X-Status-Secret` |

Systems: `tts`, `stt`, `translate`, `chat`, `ocr`, `dashboard`.

Status is **reported to** this service, since it cannot observe the AI services
itself. Three sources, all writing the same record:

* **heartbeat** — a service pings on a schedule. Works behind NAT.
* **probe** — set `STATUS_PROBE_URLS=tts=https://…/health,stt=https://…/health`
  and a background task polls them. Empty by default, so it costs nothing.
* **manual** — `PUT /status/{service}` for incidents and maintenance.

> **Silence reads `unknown`, never `operational`.** A heartbeat or probe older
> than `STATUS_STALE_AFTER_SECONDS` (default 300) makes the service read
> `unknown`. A page that claims everything is fine because nothing reported in
> is worse than one admitting it does not know — that is the classic status
> page failure. Manual statuses are exempt: a human declaring an outage is not
> quietly undone by a timer.

The 90-day strip records the **worst** status each day, not the latest, so a
recovered outage stays visible instead of erasing itself.

Writes need `STATUS_WEBHOOK_SECRET`; unset means nothing can write at all.
Anyone able to set "operational" could hide a real outage from every customer.

**Nothing reports yet?** Every system reads `unknown` until something does. To
state the truth by hand:

```bash
python -m scripts.set_status                     # all systems operational
python -m scripts.set_status down "DB failover"  # all systems down
python -m scripts.set_status --service tts degraded "Slow synthesis"
```

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
pip install -r requirements-dev.txt
python -m scripts.smoke_b2b     # 300 checks: accounts, keys, usage, credits, billing, email
python -m scripts.smoke_test    # 61 checks: blogs, subscriptions, contest, demo, status
```

**Both suites ignore your `.env`.** They set `CHATBUCKET_IGNORE_DOTENV=1` before
importing the app and pin every setting they assert on at the top of the script.
A suite that inherited the developer's config would test a different thing on
every machine: `SIGNUP_BONUS_CREDITS=1000` in one `.env` starts each account
with credits the balance assertions expect it not to have, failing twelve
checks that pass in CI — where no `.env` exists. Real process environment
variables still win, so CI can override; only the file is skipped.

To change what a suite assumes, edit the pinned block rather than your `.env`.

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
| `RATE_LIMIT_ENABLED` | `true` | Enforce the limits above. |
| `ENFORCE_PLAN_RATE_LIMITS` | `true` | Apply each plan's req/min to `POST /usage`. |
| `REQUIRE_EMAIL_VERIFICATION` | `false` | Block API-key creation until the address is confirmed. |
| `VERIFICATION_TOKEN_EXPIRE_HOURS` | `48` | Verification-link lifetime. |
| `TRUST_PROXY_HEADERS` | `false` | Believe `X-Forwarded-For`. Only behind a proxy you control. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Refresh-token lifetime. |
| `ENFORCE_CREDIT_BALANCE` | `true` | Debit credits on `POST /usage` and 402 when exhausted. |
| `SIGNUP_BONUS_CREDITS` | `0` | Credits granted to a new account. |
| `BILLING_WEBHOOK_SECRET` | – | Required by the payment-confirm webhook; unset ⇒ 503. |
| `TERMS_VERSION` | `v1` | Stamped on each account at signup. Bump when the terms change. |
| `STATUS_WEBHOOK_SECRET` | – | Required to write any status; unset ⇒ 503. |
| `STATUS_STALE_AFTER_SECONDS` | `300` | Silence beyond this reads `unknown`. |
| `STATUS_PROBE_URLS` | – | `key=url` pairs to poll. Empty disables probing. |
| `STATUS_PROBE_INTERVAL_SECONDS` | `60` | How often to poll them. |
| `CORS_ORIGINS` | localhost + chatbucket domains | Allowed browser origins. |
| `ENVIRONMENT` | `development` | In `development`, `forgot-password` returns the reset token. **Set this to `production` when deploying** — the default is a dev value, so leaving it unset both exposes reset tokens and skips the `JWT_SECRET` check. |
| `EMAIL_BACKEND` | `auto` | `auto` / `smtp` / `console` / `memory` / `disabled` — see **Email**. |
| `SMTP_HOST` | – | Provider host. Required in production unless `EMAIL_BACKEND=disabled`. |
| `SMTP_PORT` | `587` | `587` for STARTTLS, `465` for implicit TLS. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | – | Provider credentials (SendGrid's username is literally `apikey`). |
| `SMTP_USE_SSL` | `false` | Set `true` (and `SMTP_STARTTLS=false`) for port 465. |
| `SMTP_STARTTLS` | `true` | Upgrade a plaintext connection; correct for port 587. |
| `SMTP_TIMEOUT_SECONDS` | `15` | Per-send socket timeout. |
| `EMAIL_FROM` | `no-reply@chatbucket.chat` | `From:` address — must be verified with your provider. |
| `EMAIL_FROM_NAME` | `ChatBucket` | Display name on the `From:` header. |
| `SALES_EMAIL` | – | Where demo requests are sent. Empty skips the notification. |
| `APP_BASE_URL` | `http://localhost:3000` | Site hosting the reset page; used to build the reset link. |
| `PASSWORD_RESET_PATH` | `/reset-password` | Path appended to `APP_BASE_URL`. |

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
* **Forgot-password** emails a reset link (see **Email**). It also returns the
  token in the response in development, where the `console` backend means there
  is no inbox to read it out of.
* **Token revocation**: users carry a `token_version`, echoed in each JWT's
  `ver` claim and checked on every authenticated request. Resetting or changing
  a password increments it, so tokens issued earlier stop working immediately
  rather than staying valid for the rest of their 24h lifetime. API keys are a
  separate credential and are unaffected — revoke them via `/api-keys/{id}`.
* The blog/subscription/contest modules are the `chatbucket-web` site API; if
  this repo should be B2B-only, they can be dropped without touching the
  platform code.
