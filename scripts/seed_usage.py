"""Seed an account with usage history, so the dashboard has something to draw.

Usage:
    python -m scripts.seed_usage --email you@example.com
    python -m scripts.seed_usage --email you@example.com --days 60 --per-day 12
    python -m scripts.seed_usage --email you@example.com --clear

Reads MONGODB_URI / B2B_DB_NAME from the environment (or .env) exactly like the
app, and prices every record through `pricing.py`, so the seeded rows are
indistinguishable from ones `POST /usage` would have written.

Why this writes to Mongo rather than calling the API: the endpoint stamps
`created_at` with the moment it runs, so a whole month of history would land in
a single bucket and every chart would be one bar. Backdating is the entire
point, and it is the one thing the API is right to refuse.

**Seeded rows are marked** `seeded: true`, which is what `--clear` deletes.
Real usage is never touched by it, so a demo can be set up and taken down on an
account that also has genuine traffic.

The data is deliberately *not* random noise: each model keeps a plausible
shape — synthesis in bursts, transcription steady on weekdays — because a chart
of uniform random values looks broken in a way that hides real layout bugs.
"""
from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, timedelta, timezone

from app import credits, database, money
from app.pricing import calculate_cost, get_service, normalize_model_key, resolve_rate

# Model -> the rate-card service it is billed under, its engine key, and a
# plausible per-request size. Mirrors the four engines the dashboard shows.
PROFILES = [
    # (model, service, engine, min_qty, max_qty, weekday_bias, share)
    ("CB Paluku", "tts_streaming", "cb_paluku", 400, 3200, 0.6, 0.35),
    ("CB Vinu", "stt_offline", "cb_vinu", 1.5, 18.0, 0.9, 0.30),
    ("CB Vinu", "stt_streaming", "cb_vinu", 0.5, 9.0, 0.9, 0.10),
    ("CB Vaaradhi", "translation", "cb_vaaradhi", 800, 12000, 0.7, 0.15),
    ("CB Thodu", "chat_agent", "cb_thodu", 500, 9000, 0.5, 0.10),
]


async def resolve_user(email: str) -> dict:
    user = await database.users_collection().find_one({"email": email.lower().strip()})
    if user is None:
        raise SystemExit(f"No account with email {email!r}. Sign up first.")
    return user


# Every ledger entry this script writes is described with this prefix, which is
# how `--clear` finds its own work again without a schema change.
SEED_DESCRIPTION = "Seeded demo"


async def clear(user_id) -> tuple[int, int]:
    """Remove seeded usage and undo the credits that went with it.

    The balance is corrected by the exact net of the entries being deleted,
    rather than recomputed from the ledger: an account may hold real purchases
    and real spend, and rebuilding its balance from scratch would quietly
    rewrite figures this script never wrote.
    """
    usage_removed = (
        await database.usage_collection().delete_many(
            {"user_id": user_id, "seeded": True}
        )
    ).deleted_count

    ledger = database.credit_ledger_collection()
    entries = await ledger.find(
        {"user_id": user_id, "description": {"$regex": f"^{SEED_DESCRIPTION}"}}
    ).to_list(length=None)
    if not entries:
        return usage_removed, 0

    net = sum(int(e["units"]) for e in entries)
    granted = sum(int(e["units"]) for e in entries if e["kind"] == credits.KIND_PURCHASE)
    await database.credit_accounts_collection().update_one(
        {"user_id": user_id},
        {"$inc": {"balance_units": -net, "lifetime_purchased_units": -granted}},
    )
    await ledger.delete_many({"_id": {"$in": [e["_id"] for e in entries]}})
    return usage_removed, len(entries)


def build_records(user_id, days: int, per_day: int, rng: random.Random) -> list[dict]:
    """One flat list of priced usage documents spread across `days`."""
    now = datetime.now(timezone.utc)
    documents: list[dict] = []

    weights = [p[6] for p in PROFILES]
    for day_offset in range(days):
        day = now - timedelta(days=day_offset)
        # A quiet weekend reads as a real usage pattern; a flat line reads as a
        # bug in the chart.
        weekend = day.weekday() >= 5
        count = max(0, int(rng.gauss(per_day * (0.45 if weekend else 1.0), per_day * 0.3)))

        for _ in range(count):
            model, service_key, engine, low, high, bias, _share = rng.choices(
                PROFILES, weights=weights, k=1
            )[0]
            if weekend and rng.random() > (1.0 - bias) + 0.35:
                continue

            service = get_service(service_key)
            quantity = round(rng.uniform(low, high), 2 if high <= 100 else 0)
            if quantity <= 0:
                continue

            rate, unit_size, _ = resolve_rate(service_key, model)
            cost = calculate_cost(service_key, quantity, model)

            # Spread across the working day so the hourly view has shape too.
            created = day.replace(
                hour=rng.randint(7, 21),
                minute=rng.randint(0, 59),
                second=rng.randint(0, 59),
                microsecond=0,
            )
            documents.append({
                "user_id": user_id,
                "api_key_id": None,
                "project_id": None,
                "service": service.key,
                "label": service.label,
                "unit": service.unit,
                "quantity": float(quantity),
                "pricing": "flat",
                "input_quantity": None,
                "output_quantity": None,
                "input_rate": None,
                "output_rate": None,
                "rate": money.to_bson(rate),
                "unit_size": unit_size,
                "cost": money.to_bson(cost),
                "currency": "INR",
                "model": model,
                "model_key": normalize_model_key(model),
                # The engine's meter runs slightly ahead of ours — it counts
                # what it processed, we bill what was sent.
                "engine": engine,
                "engine_quantity": round(float(quantity) * rng.uniform(1.02, 1.12), 3),
                "metadata": {"source": "seed"},
                "billed": True,
                "created_at": created,
                "seeded": True,
            })

    return documents


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Account to seed.")
    parser.add_argument("--days", type=int, default=30, help="How far back to go.")
    parser.add_argument("--per-day", type=int, default=8, help="Average records per day.")
    parser.add_argument("--clear", action="store_true", help="Remove seeded rows and exit.")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed, for repeatable data.")
    parser.add_argument(
        "--topup",
        type=float,
        default=2000.0,
        help="Credits to grant before the spend is debited (INR).",
    )
    parser.add_argument(
        "--no-debit",
        action="store_true",
        help="Skip the credit debit + ledger entries.",
    )
    args = parser.parse_args()

    await database.connect()
    try:
        user = await resolve_user(args.email)
        user_id = user["_id"]

        removed, entries = await clear(user_id)
        if args.clear:
            print(
                f"Removed {removed} seeded record(s) and {entries} ledger "
                f"entr{'y' if entries == 1 else 'ies'} for {args.email}."
            )
            return 0
        if removed:
            print(f"Replaced {removed} previously seeded record(s).")

        rng = random.Random(args.seed)
        documents = build_records(user_id, args.days, args.per_day, rng)
        if not documents:
            print("Nothing generated — try a larger --per-day.")
            return 1

        await database.usage_collection().insert_many(documents)
        total = sum(money.to_json(d["cost"]) for d in documents)

        # Keep the Billing page consistent with the Usage page. Without this the
        # dashboard shows spend that never came out of the balance, which looks
        # like a billing bug rather than seeded data.
        #
        # The top-up is deliberately larger than the spend: granting exactly
        # what was consumed leaves a zero balance, and a "Credits Available:
        # ₹0.00" tile is indistinguishable from the empty dashboard this script
        # exists to fill. It is booked as a purchase so it also counts towards
        # `lifetime_purchased`, which is the denominator of the "% remaining"
        # bar — without it the bar has nothing to be a fraction of.
        if not args.no_debit:
            spent = credits.to_units(total)
            topup = max(credits.to_units(args.topup), spent * 2)
            await credits.grant(
                user_id, topup, credits.KIND_PURCHASE, f"{SEED_DESCRIPTION} top-up"
            )
            await credits.debit_allowing_negative(
                user_id, spent, f"{SEED_DESCRIPTION} usage"
            )

        days_used = len({d["created_at"].date() for d in documents})
        print(
            f"Seeded {len(documents)} usage record(s) across {days_used} day(s) "
            f"for {args.email} — total INR {total:.2f}."
        )
        print("Remove them again with:  python -m scripts.seed_usage "
              f"--email {args.email} --clear")
        return 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
