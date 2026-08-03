"""Plan catalogue: rate limits and top-up packs.

Single source of truth for what each plan allows, the way `pricing.py` is for
what each service costs.

The product is **prepaid credits, not subscriptions** — the pricing page says
"add credits anytime, no subscriptions, credits never expire". So a "plan" here
is not a recurring charge: buying a pack grants credits *and* moves the account
onto that pack's rate-limit tier. `starter` is the pay-as-you-go floor everyone
begins on.

1 credit = ₹1, which is what makes `₹10,000 -> 11,000 credits` a 10% bonus.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    # Rate limits are advertised here and reported by `GET /limits`. Enforcing
    # them needs a shared counter across workers and is NOT implemented yet —
    # see the README. Reporting a limit the gateway does not apply is a
    # deliberate, documented gap, not an oversight.
    requests_per_minute: int
    concurrency: int
    support: str
    best_for: str
    # What buying this pack costs and grants. Zero for `starter`, which is not
    # purchasable — you are on it by default.
    price_inr: Decimal
    credits_granted: Decimal


PLANS: dict[str, Plan] = {
    p.key: p
    for p in [
        Plan(
            key="starter",
            label="Starter",
            requests_per_minute=60,
            concurrency=2,
            support="Community support",
            best_for="Prototyping & testing",
            price_inr=Decimal("0"),
            credits_granted=Decimal("0"),
        ),
        Plan(
            key="pro",
            label="Pro",
            requests_per_minute=200,
            concurrency=10,
            support="Email support",
            best_for="Startups & POCs",
            price_inr=Decimal("10000"),
            credits_granted=Decimal("11000"),  # 10% bonus
        ),
        Plan(
            key="business",
            label="Business",
            requests_per_minute=1000,
            concurrency=50,
            support="Email support",
            best_for="Production workloads",
            price_inr=Decimal("50000"),
            credits_granted=Decimal("57500"),  # 15% bonus
        ),
    ]
}

DEFAULT_PLAN = "starter"
# Everything except the free tier can be bought as a top-up pack.
PURCHASABLE = tuple(k for k, p in PLANS.items() if p.price_inr > 0)


class UnknownPlanError(ValueError):
    pass


def get_plan(key: str | None) -> Plan:
    """Resolve a plan key, falling back to the default.

    Accounts created before plans existed carry no `plan` field; they read as
    `starter` rather than erroring.
    """
    plan = PLANS.get((key or DEFAULT_PLAN).lower())
    if plan is None:
        raise UnknownPlanError(
            f"Unknown plan '{key}'. Valid: {', '.join(PLANS)}"
        )
    return plan


def plan_catalogue() -> list[dict]:
    """JSON-serialisable description of every plan."""
    from . import money

    return [
        {
            "plan": p.key,
            "label": p.label,
            "requests_per_minute": p.requests_per_minute,
            "concurrency": p.concurrency,
            "support": p.support,
            "best_for": p.best_for,
            "price": money.to_json(p.price_inr),
            "credits": money.to_json(p.credits_granted),
            "bonus_credits": money.to_json(p.credits_granted - p.price_inr),
            "purchasable": p.price_inr > 0,
        }
        for p in PLANS.values()
    ]
