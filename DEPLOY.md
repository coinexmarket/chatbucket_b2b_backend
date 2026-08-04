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
doctl apps create-deployment 37f4ec6f-3fbf-4758-9d2e-e52bfefdbad2 --wait
```

**The second line is not optional.** The app is wired to this repo as a plain
git clone URL rather than through DigitalOcean's GitHub app, so nothing
webhooks a push — a merge to `main` changes nothing until a deployment is asked
for. That was the deliberate trade for setting the app up without granting a
third party write-scoped access to the GitHub account; authorize the GitHub
integration in the DO dashboard and set `deploy_on_push: true` if you would
rather have it automatic.

A deployment builds the image, starts it, and only shifts traffic once
`/health` answers 200. A failed build or a failed health check leaves the
previous instance serving.

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
`ChatBucketHackathon`) on first write, alongside the `cb_prod` database other
services already use on the same cluster.

The URI is the `mongodb+srv://` form, which resolves through DNS and therefore
needs `dnspython` — that is why it is pinned in `requirements.txt` even though
nothing imports it. Drop it and the image still builds; it fails at connect
time instead.

The cluster currently has **no trusted sources configured**, which means it
accepts connections from any address with a valid password. Restricting it to
the app is a dashboard change (Databases → Settings → Trusted sources) and is
worth doing.

## What is deliberately unset

* **`EMAIL_BACKEND=disabled`** — this deployment sends no mail at all. Password
  reset returns its usual 200 and no link is delivered; demo requests are
  recorded but sales is not notified. Set `SMTP_HOST` and the credentials, then
  switch to `auto`, before either matters to a real customer. `disabled` is
  chosen over `auto` on purpose: `auto` with no host would log reset links to
  stdout, which looks exactly like a working password reset.
* **Razorpay is not configured.** `POST /billing/top-up` creates a local pending
  payment and nothing else; settlement is by the shared-secret confirm endpoint
  only. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and
  `RAZORPAY_WEBHOOK_SECRET` to turn Checkout on.
* **No custom domain.** The app answers on its `*.ondigitalocean.app` hostname.
  To serve `api.chatbucket.business`, add the domain in the DO dashboard and
  point a CNAME at that hostname from wherever `chatbucket.business` is hosted —
  the domain is not on DigitalOcean DNS, so App Platform cannot create the
  record itself.
* **`ENGINE_FREE_QUOTAS`** — unset, so engine burn is counted but `remaining`
  and `percent_used` read null. See the README.

## CORS

`CORS_ORIGINS` is set explicitly in production and **drops the localhost dev
origins** that the built-in default carries. A dashboard running on
`localhost:3000` therefore cannot call this deployment; point local development
at a local backend, or add the origin knowingly.

## Rolling back

```bash
doctl apps list-deployments 37f4ec6f-3fbf-4758-9d2e-e52bfefdbad2
doctl apps create-deployment 37f4ec6f-3fbf-4758-9d2e-e52bfefdbad2 --deployment-id <previous>
```

Rolling back the code does **not** roll back the database. Index creation is
additive and safe to leave; a migration that rewrote records would not be.

For the order in which the AI services must be cut over to metering, see
[ROLLOUT.md](ROLLOUT.md).
