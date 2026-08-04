# Metering rollout

Bringing per-customer metering live across the AI services, in an order where
nothing goes down.

The risk is not the code — it is the sequencing. Three services now refuse
callers that present no API key, and two pairs of repos have to ship together
or they silently record nothing. Deploy in the order below and each step is
reversible on its own.

**The rule throughout: issue keys before enforcing them.** Every service ships
able to run in a permissive mode where an unkeyed caller is still served and
logged. Use it. Move traffic across, watch the warnings stop, *then* close the
door.

---

## Phase 0 — Before anything deploys

### 0.1 Configure the platform

| Variable | Value | Why |
|---|---|---|
| `ENVIRONMENT` | `production` | Otherwise reset tokens are returned in responses and the `JWT_SECRET` check is skipped. |
| `JWT_SECRET` | a real secret | The app refuses to start on the default in production. |
| `OPS_SECRET` | a strong secret | Without it `GET /engines/usage` returns 503 and you cannot see engine burn. |
| `BILLING_WEBHOOK_SECRET` | a strong secret | Without it **no route can grant credits**. |
| `ENGINE_FREE_QUOTAS` | e.g. `cb_vinu=12000,cb_paluku=100000` | Optional. Unset means burn is counted but `remaining` reads null. |
| `CORS_ORIGINS` | your dashboard origins | Matched exactly, including port. |

### 0.2 Issue a key per customer

Each customer needs one from the dashboard (**API Keys → Create**). The
plaintext `cb_live_…` is shown **once**; only its hash is stored, so there is
no way to recover it later. Record which customer got which key — you will need
that to chase stragglers in Phase 3.

### 0.3 Give every account credits

`POST /usage` returns **402** when a customer cannot pay, and with
`ENFORCE_CREDIT_BALANCE=true` (the default) that stops their calls. Existing
accounts have a zero balance.

Either top them up (`POST /billing/top-up` then confirm with
`X-Billing-Secret`), or set `ENFORCE_CREDIT_BALANCE=false` to meter without
blocking while you migrate.

---

## Phase 1 — Deploy the platform

Deploy `chatbucket_b2b_backend` **first and alone**.

Everything in it is additive: `POST /api-keys/verify` is new, `provider` and
`engine` are optional fields, and `POST /usage` still accepts an API key
exactly as before. Nothing that works today stops working.

**Verify**

```bash
curl -X POST https://<api>/api-keys/verify -H "X-API-Key: cb_live_…"
# 200 with user_id, plan, credits, has_credits

curl -H "X-Ops-Secret: $OPS_SECRET" https://<api>/engines/usage
# 200, every engine listed, all zero
```

**Rollback:** redeploy the previous image. Nothing else depends on this yet.

---

## Phase 2 — Deploy the services, permissive

Each service, in any order. Set the platform URL, and **explicitly disable
enforcement** — the code now defaults to closed, so this is a deliberate
override for the migration window.

| Service | Required | Migration override |
|---|---|---|
| `speech_to_text_py_4lang` | `CHATBUCKET_API_URL` | `STT_REQUIRE_API_KEY=false` |
| `translation_gateway` | `CHATBUCKET_API_URL` | `TRANSLATE_REQUIRE_API_KEY=false` |
| `conversational_chatAgents` | `CHATBUCKET_API_URL` | `CHAT_ALLOW_SHARED_SECRET=true` |
| `Voice_agents` | `CHATBUCKET_API_URL` | — (skips unmapped orgs by design) |

Optional everywhere: `CB_VINU_PROVIDER`, `CB_PALUKU_PROVIDER`,
`CB_VAARADHI_PROVIDER`, `CB_THODU_PROVIDER` — the upstream serving that
deployment, for reconciling supplier invoices. Unset means usage is recorded
under `(unreported)`.

### Two pairs that must ship together

**`Translation_pipeline` + `translation_gateway`.** The gateway bills the token
counts the translation services report. Deployed from an older image they
report nothing, the gateway sums zero, and **no translation usage is recorded
at all** — with no error to notice.

**`conversational_chatAgents` + `cb_b2b`.** The service now mounts
`AuthMiddleware`, which was previously never registered; the chat page sends
the session header as of the matching commit. Either alone breaks the chat
playground.

### `Voice_agents` only

- Add `CHATBUCKET_METERING=off` to `api/.env.test`. It is gitignored, so the
  line in `.env.test.example` will not reach an existing file. Without it,
  `test_workflow_run_cost` makes a database call it never used to.
- Map each organization to a ChatBucket account:

```python
await db_client.upsert_configuration(
    organization_id=42, key="chatbucket", value={"api_key": "cb_live_…"}
)
```

Unmapped organizations are skipped and logged at debug, so partial mapping is
safe.

**Verify** — make one real call per service, then:

```bash
curl -H "X-Ops-Secret: $OPS_SECRET" https://<api>/engines/usage
```

Each engine you exercised should show non-zero `consumed`, with the calling
account named in `top_accounts`.

**Rollback:** redeploy the previous image, or set `CHATBUCKET_METERING=off` to
stop reporting while leaving the service running.

---

## Phase 3 — Move callers onto keys

Every service logs the callers that have not moved:

```
unmetered /transcribe — caller presented no API key
unmetered translate — no API key presented
unmetered request to /api/chat — caller is using the shared secret
```

Work through them: give each integration its key, have it send `X-API-Key`
(or `?api_key=` on the STT WebSocket — the browser cannot set headers there).

**Do not proceed until those warnings stop.** They are the list of things
Phase 4 will break.

Watch too for `customer … is out of credits` — that is a 402, meaning the usage
was recorded but not billed. `GET /usage/summary` reports it as
`unbilled_total`.

---

## Phase 4 — Close the door

Remove the Phase 2 overrides, one service at a time, leaving a day between:

```
STT_REQUIRE_API_KEY        → unset (defaults true)
TRANSLATE_REQUIRE_API_KEY  → unset (defaults true)
CHAT_ALLOW_SHARED_SECRET   → unset (defaults false)
```

**Verify** an unkeyed call is now refused:

```bash
curl -X POST https://<stt>/transcribe -F "file=@clip.wav"        # 401
curl -X POST https://<translate>/translate -d '{"text":"hi"}'    # 401
```

**Rollback:** set the variable back. It is one environment change, no redeploy
of code.

---

## What to watch afterwards

| Signal | Where | Means |
|---|---|---|
| `unbilled_total > 0` | `GET /usage/summary` | Someone consumed without credits. |
| `(unreported)` in `by_provider` | `GET /engines/usage` | A service has no `CB_*_PROVIDER` set. |
| `exhausted: true` | `GET /engines/usage` | An allowance is spent. |
| `unattributed_cost > 0` | `GET /usage/summary` | Something metered without sending `model`. |
| `chatbucket.metering` errors | service logs | Usage was consumed but not recorded. |

---

## Known gaps at the time of writing

These are deliberate, not oversights:

- **Two playground pages are not metered.** Translate calls
  `nllb.chatbucket.chat` and the voice agent calls
  `voiceagentsdk.chatbucket.chat` — neither is one of the services wired here,
  so CB Vaaradhi and CB Thodu stay empty until they are.
- **Live STT minutes are client-reported.** The browser streams straight to the
  engine, so elapsed time is only knowable there. Acceptable for billing — it
  is the customer's own account — but it makes the engine allowance a floor
  rather than a fact.
- **Auto-recharge never fires**, **GST is not computed on invoices**, and
  **plan concurrency is reported but not enforced.** See the README.
- **Most request handlers ship unexercised.** Metering clients were driven
  against a live platform; the code inside the STT handlers, the two
  IndicTrans2 token-count blocks and the `Voice_agents` hook has not been run,
  because this repository's development environment has neither the models nor
  the GPU dependencies. Smoke each service once in an environment that does.
