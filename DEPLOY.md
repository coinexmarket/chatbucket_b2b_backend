# Deploying

The service runs on **DigitalOcean App Platform**, built from the `Dockerfile`
in this repo against the managed MongoDB cluster `cb-db-mongodb-pord` (blr1).

| | |
| --- | --- |
| App | `chatbucket-b2b-backend` (`37f4ec6f-3fbf-4758-9d2e-e52bfefdbad2`) |
| Region | `blr` — the same region as the database, so the hop is local |
| Instance | `apps-s-1vcpu-1gb`, 1 instance |
| Branch | `main` |
| Spec | [.do/app.yaml](.do/app.yaml) — secrets redacted, see **Secrets** |

## Deploying a change

```bash
git push origin main
```

That is the whole procedure. [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)
lints, runs both suites on Python 3.12 and 3.13, builds the image and pushes it
to `registry.digitalocean.com/chatbucket/b2b-backend`. App Platform subscribes
to that repository and redeploys itself when a new `latest` arrives.

**App Platform does not build anything.** It runs the image CI published, which
is why the tests are a gate rather than a report: a red `main` never produces an
image, so it cannot reach production. Wiring App Platform straight to the GitHub
repository would have removed that gate — it deploys whatever lands on the
branch.

Every build is tagged twice: `latest`, which the app follows, and the commit
SHA, which is immutable. `latest` is pushed **last**, so the tag that triggers a
deploy never moves before the rollback target is safely stored.

A deployment starts the new image and only shifts traffic once `/health`
answers 200; a failed health check leaves the previous instance serving.

To deploy without a commit, run the workflow from the Actions tab
(`workflow_dispatch`). To deploy by hand, bypassing the tests entirely:

```bash
doctl apps create-deployment 37f4ec6f-3fbf-4758-9d2e-e52bfefdbad2 --wait
```

### CI credentials

The workflow needs one repository secret, **`DIGITALOCEAN_ACCESS_TOKEN`**, used
to log in to the registry. Scope it to the registry and apps rather than issuing
a full-access token: it lives in CI, and a token that can also delete droplets
and rewrite DNS is a much worse thing to leak than one that can push an image.

## Secrets

`.do/app.yaml` is committed with `${PLACEHOLDER}` in every secret slot, because
**this repository is public**. The live values are held encrypted by App
Platform (`type: SECRET`) and are readable in the DO dashboard under
Settings → App-Level Environment Variables.

To rotate one:

```bash
doctl apps update 37f4ec6f-3fbf-4758-9d2e-e52bfefdbad2 --spec <(...)   # or the dashboard
```

Rotating `JWT_SECRET` signs every customer out at once — every issued access
token fails its signature check immediately. That is the correct response to a
suspected leak and a poor way to spend a Tuesday otherwise.

## The database credential

The app authenticates as **`cb-b2b-app`**, a user created for it, not as
`doadmin`. A deployment that holds the cluster's superuser credential can drop
every other product's data by typo; this one can only reach what it writes.

It creates its three logical databases (`chatbucket_b2b`, `chatbucket`,
`ChatBucketHackathon`) on first write.

The cluster is **`cb-b2b-mongodb-pord`**, dedicated to this service — three
nodes in `blr1`, so a node failure fails over rather than taking billing down.
It was moved off the shared `cb-db-mongodb-pord` while both were empty, which
is the only time such a move is free: no export, no import, no window during
which a write could land on the cluster being left behind. Indexes rebuild
themselves on first start, so nothing had to be recreated by hand.

The URI is the `mongodb+srv://` form, which resolves through DNS and therefore
needs `dnspython` — that is why it is pinned in `requirements.txt` even though
nothing imports it. Drop it and the image still builds; it fails at connect
time instead.

The cluster currently has **no trusted sources configured**, which means it
accepts connections from any address with a valid password. Restricting it to
the app is a dashboard change (Databases → Settings → Trusted sources) and is
worth doing.

## What is deliberately unset

* **Razorpay runs on test keys** (`rzp_test_…`), so no real money moves. The
  whole path works — order creation, Checkout, webhook, credits, invoice — but
  only with test cards. Going live is swapping `RAZORPAY_KEY_ID` and
  `RAZORPAY_KEY_SECRET` for their `rzp_live_` counterparts and re-registering
  the webhook, which is a **separate** endpoint on the live dashboard with its
  own secret.
* **`ENFORCE_CREDIT_BALANCE`** is left at its default of `true`. That is only
  safe because this deployment started with an empty database — on an install
  with existing accounts it would 402 every one of them, all at once.
* **`ENGINE_FREE_QUOTAS`** — unset, so engine burn is counted but `remaining`
  and `percent_used` read null. See the README.

## Mail

Mail goes out through Google Workspace as `Support@chatbucket.business`, over
`smtp.gmail.com:587` with STARTTLS. `EMAIL_BACKEND=smtp` rather than `auto`, so
a missing `SMTP_HOST` fails loudly at startup instead of quietly falling back to
logging reset links to stdout.

`SMTP_PASSWORD` is a Google **App Password**, not the account's login password —
Google stopped accepting the latter for SMTP, and no amount of correctness in
the value will make it authenticate. It is tied to 2-Step Verification on that
mailbox, and revoking it in the Google account is what turns off this
deployment's ability to send.

It authenticates as a plain mailbox, deliberately not the Workspace admin: this
credential lives in an environment variable that anyone with dashboard access
can read, and it should not be able to send as an administrator.

`EMAIL_FROM` matches `SMTP_USERNAME` because Google rewrites a `From` header
that is neither the authenticated account nor one of its verified aliases — the
mail still arrives, from an address the customer was not expecting.

Two messages are sent: the password-reset link, and the demo-request
notification to `SALES_EMAIL` (also `Support@`). Neither can fail a request;
a mail outage is logged under `chatbucket_b2b.email` and nothing else.

## Payments

The webhook is registered in the Razorpay dashboard against
`https://api.b2b.chatbucket.business/billing/webhook/razorpay`, subscribed to
`payment.captured` and `payment.authorized` — the only two events the handler
acts on; anything else is acknowledged and ignored.

`RAZORPAY_WEBHOOK_SECRET` is **not** the key secret. It is a value chosen when
registering the webhook, and signing with the wrong one rejects every delivery
while looking correctly configured from both ends. The smoke suite asserts a
webhook signed with the key secret is refused, because that is the mistake
people make.

Re-registering the webhook after a key rotation is easy to forget: live and
test mode have separate webhook registrations, so switching keys without adding
the live webhook leaves payments that settle at the gateway and never credit
anyone here.

## The domain

`api.b2b.chatbucket.business` is the app's primary domain; the
`*.ondigitalocean.app` hostname keeps working alongside it.

DNS for `chatbucket.business` is at the registrar, **not** DigitalOcean, so App
Platform cannot create records itself — the subdomain is a `CNAME` to the app's
default hostname, added by hand at the registrar. Everything after that (the
TXT challenge, CA authorization, the certificate and its renewal) DigitalOcean
does on its own; a domain sitting in `CONFIGURING` at the `verify-cname` step
means the record is missing or has not propagated, not that anything is broken.

The apex `chatbucket.business` and `www` point at the `cb-b2b-pord` droplet and
are untouched by this — only the `api.b2b` subdomain is delegated here.

## CORS

`CORS_ORIGINS` is set explicitly in production and **drops the localhost dev
origins** that the built-in default carries. A dashboard running on
`localhost:3000` therefore cannot call this deployment; point local development
at a local backend, or add the origin knowingly.

## Rolling back

Retag the commit you want back as `latest` and the app redeploys itself:

```bash
doctl registry login
docker pull registry.digitalocean.com/chatbucket/b2b-backend:<sha>
docker tag  registry.digitalocean.com/chatbucket/b2b-backend:<sha> \
            registry.digitalocean.com/chatbucket/b2b-backend:latest
docker push registry.digitalocean.com/chatbucket/b2b-backend:latest
```

`doctl registry repository list-tags b2b-backend` lists what is available to go
back to. Reverting the commit and letting CI rebuild works too, and is tidier
in the history — but it takes a full test-and-build cycle, which is the wrong
order of events when production is down.

Rolling back the code does **not** roll back the database. Index creation is
additive and safe to leave; a migration that rewrote records would not be.

For the order in which the AI services must be cut over to metering, see
[ROLLOUT.md](ROLLOUT.md).
