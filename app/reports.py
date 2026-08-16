"""The monthly usage report — the figures behind `monthly_report.html`.

Kept out of `email.py` because it is arithmetic over the usage collection, not
message construction, and because the same numbers are worth having available
to anything else that wants to summarise a month.

Every figure here is computed from records this service actually stored. The
design ships with sample metrics ("conversations", "messages processed") that
this platform does not meter; rather than invent them, the four headline cards
are labelled with what *is* metered — requests, spend, voice minutes and agent
interactions — and the labels travel with the values so the card and its number
can never disagree.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import credits, emailtemplates, money
from .config import get_settings
from .database import credit_accounts_collection, usage_collection
from .plans import get_plan
from .pricing import SERVICES

# Services billed by the minute, and services that are an "agent interaction".
# Derived from the rate card rather than hard-coded, so a new minute-priced
# service joins the voice total the day it is priced.
_MINUTE_SERVICES = tuple(k for k, s in SERVICES.items() if s.unit == "minutes")
_AGENT_SERVICES = ("chat_agent", "voice_agent_web", "voip_call")

# Legend colours, in the order the design uses them.
_SERVICE_COLORS = ("#5421C7", "#7C4DEE", "#A07BF5", "#C4AAFA", "#E2D6FD")

# Short names for the report only. The legend and the share bars are narrow
# columns beside a right-aligned percentage, and the rate card's full labels
# ("Speech-to-Text (streaming)") wrap onto two lines there and collide with it.
# `pricing.py` keeps the precise names — an invoice needs to say which variant
# was billed; a chart legend does not.
_SHORT_LABELS = {
    "stt_streaming": "Speech to Text",
    "stt_offline": "Speech to Text (file)",
    "tts_streaming": "Text to Speech",
    "tts_offline": "Text to Speech (file)",
    "translation": "Translation",
    "chat_agent": "Chat Agent",
    "voice_agent_web": "Voice Agent",
    "voip_call": "Voice Agent (call)",
}


def _service_label(key: str) -> str:
    service = SERVICES.get(key)
    return _SHORT_LABELS.get(key) or (service.label if service else key)

# Height of the stacked bar beside the legend, in pixels.
_CHART_HEIGHT = 140
_MIN_SLICE = 4

_UP, _DOWN, _FLAT = "↑", "↓", "→"
_GREEN, _GREEN_BG = "#239653", "#DDF5E6"
_RED, _RED_BG = "#C2334D", "#FBE4E9"
_GREY, _GREY_BG = "#70697C", "#EFEDF4"


def month_window(when: datetime) -> tuple[datetime, datetime]:
    """The calendar month containing `when`, as ``[start, next_start)``."""
    start = when.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    # Adding 32 days and snapping back to the 1st lands on the next month for
    # every month length, which arithmetic on `month + 1` does not.
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following


def previous_month_window(start: datetime) -> tuple[datetime, datetime]:
    previous_start = (start - timedelta(days=1)).replace(day=1)
    return previous_start, start


async def _totals(user_id, begin: datetime, finish: datetime) -> dict:
    """Requests, spend, voice minutes and agent interactions for one window."""
    rows = await usage_collection().aggregate([
        {"$match": {"user_id": user_id, "created_at": {"$gte": begin, "$lt": finish}}},
        {
            "$group": {
                "_id": "$service",
                "cost": {"$sum": "$cost"},
                "quantity": {"$sum": "$quantity"},
                "requests": {"$sum": 1},
            }
        },
    ]).to_list(length=None)

    by_service = {row["_id"]: row for row in rows}
    return {
        "requests": sum(r["requests"] for r in rows),
        "cost": money.total(money.to_decimal(r.get("cost", 0)) for r in rows),
        "minutes": sum(
            by_service[k].get("quantity", 0) for k in _MINUTE_SERVICES if k in by_service
        ),
        "agent_requests": sum(
            by_service[k].get("requests", 0) for k in _AGENT_SERVICES if k in by_service
        ),
        "by_service": by_service,
    }


def _change(current: Decimal | float | int, previous: Decimal | float | int) -> tuple[str, str, str, str]:
    """``(label, arrow, colour, pill background)`` for a month-over-month move.

    With no previous month there is no percentage to state: "100%" and "0%"
    would both be inventions, so it reads "new" and renders neutral.
    """
    current, previous = Decimal(str(current)), Decimal(str(previous))
    if previous == 0:
        if current == 0:
            return "no change", _FLAT, _GREY, _GREY_BG
        return "new", _UP, _GREEN, _GREEN_BG
    percent = (current - previous) / previous * 100
    if percent > 0:
        return f"{percent:.1f}%", _UP, _GREEN, _GREEN_BG
    if percent < 0:
        return f"{abs(percent):.1f}%", _DOWN, _RED, _RED_BG
    return "0.0%", _FLAT, _GREY, _GREY_BG


def _metric(index: int, label: str, current, previous, render) -> dict:
    """One headline card.

    `current` and `previous` are the raw figures — the direction of travel is
    computed from those, never from `render`ed text, or "₹1,000" would compare
    as smaller than "₹900".
    """
    text, arrow, color, background = _change(current, previous)
    return {
        f"metric{index}_label": label,
        f"metric{index}_value": render(current),
        f"metric{index}_previous": render(previous),
        f"metric{index}_change": text,
        f"metric{index}_arrow": arrow,
        f"metric{index}_color": color,
        f"metric{index}_background": background,
    }


def _count(value) -> str:
    return f"{int(value):,}"


def _amount(value) -> str:
    return f"{emailtemplates.currency_symbol()}{money.to_json(value):,.2f}"


def _round_quantity(value) -> str:
    """Minutes read as whole numbers unless the month really was fractional."""
    quantity = round(float(value), 1)
    return f"{int(quantity):,}" if quantity == int(quantity) else f"{quantity:,.1f}"


def _top_services(by_service: dict, total_cost: Decimal) -> list[dict]:
    """The five biggest services by spend, with share, colour and bar height."""
    ranked = sorted(
        ((key, money.to_decimal(row.get("cost", 0))) for key, row in by_service.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )[:5]

    services = []
    for position, (key, cost) in enumerate(ranked):
        share = float(cost / total_cost * 100) if total_cost else 0.0
        services.append({
            "name": _service_label(key),
            "percent": f"{share:.0f}",
            "value": _amount(cost),
            "color": _SERVICE_COLORS[position % len(_SERVICE_COLORS)],
            # A service with a rounding-error share still needs a visible
            # sliver, or the bar silently loses a row the legend still lists.
            "bar_height": max(round(_CHART_HEIGHT * share / 100), _MIN_SLICE),
        })
    return services


def _insights(current: dict, previous: dict, services: list[dict]) -> dict:
    """Three plain observations, drawn from the same figures as the cards."""
    spend_change, _, _, _ = _change(current["cost"], previous["cost"])
    grew = current["cost"] > previous["cost"]

    if previous["requests"] == 0 and current["requests"] > 0:
        first = ("You're off the ground!", "This is your first month of metered usage. Nice work.")
    elif grew:
        first = ("You're Growing!", f"Your spend is up {spend_change} on last month. Keep going!")
    elif current["requests"] == 0:
        first = ("A quiet month", "No metered calls this month. We're here when you need us.")
    else:
        first = ("Steady month", f"Your spend moved {spend_change} against last month.")

    if current["agent_requests"]:
        second = (
            "Automation at work",
            f"Agents handled {current['agent_requests']:,} interactions this month.",
        )
    else:
        second = (
            "Try an agent",
            "Chat and voice agents can handle the repetitive conversations for you.",
        )

    if services:
        third = (
            "Pro Tip",
            f"{services[0]['name']} is your biggest line. Batch those calls to cut cost.",
        )
    else:
        third = (
            "Pro Tip",
            "Start with a single prompt and let the builder assemble the agent for you.",
        )

    return {
        "insight1_title": first[0], "insight1_text": first[1],
        "insight2_title": second[0], "insight2_text": second[1],
        "insight3_title": third[0], "insight3_text": third[1],
    }


async def build_monthly_report(user: dict, month_start: datetime) -> dict:
    """Everything `monthly_report.html` needs for one account and one month."""
    settings = get_settings()
    begin, finish = month_window(month_start)
    previous_begin, previous_finish = previous_month_window(begin)

    current = await _totals(user["_id"], begin, finish)
    previous = await _totals(user["_id"], previous_begin, previous_finish)

    plan = get_plan(user.get("plan"))
    account = await credit_accounts_collection().find_one({"user_id": user["_id"]}) or {}
    balance = credits.from_units(int(account.get("balance_units", 0)))

    services = _top_services(current["by_service"], current["cost"])

    # The three bars under "Your Plan & Usage". The design's quota bars do not
    # apply — this is prepaid credit, there is no monthly allowance to fill —
    # so they show each top service's share of the month's spend instead, which
    # is a proportion that genuinely has a denominator.
    total_label = _amount(current["cost"])
    bars: dict = {}
    for index in range(1, 4):
        service = services[index - 1] if len(services) >= index else None
        bars[f"bar{index}_label"] = service["name"] if service else "-"
        bars[f"bar{index}_percent"] = service["percent"] if service else "0"
        bars[f"bar{index}_amount"] = (
            f"{service['value']} / {total_label}" if service else "-"
        )

    # `finish` is exclusive, so the last day of the month is the day before it.
    last_day = finish - timedelta(days=1)
    report = {
        "period": (
            f"{emailtemplates.fmt_date(begin)} - {emailtemplates.fmt_date(last_day)}"
        ),
        "previous_period": emailtemplates.fmt_month(previous_begin),
        "generated_on": emailtemplates.fmt_date(datetime.now(timezone.utc)),
        "plan_name": plan.label,
        # Prepaid: an account is "active" while it can still pay for a call.
        "plan_status": "Active" if balance > 0 else "No credits",
        "analytics_url": settings.dashboard_url_for,
        "upgrade_url": settings.dashboard_url_for,
        "services": services,
        # Two halves of one sentence in the design; the second is emphasised.
        # Congratulating an account on a month its usage fell would be worse
        # than saying nothing, so both halves move together.
        "headline_note": (
            "Your usage grew against last month."
            if current["cost"] > previous["cost"]
            else f"Measured against {emailtemplates.fmt_month(previous_begin)}."
        ),
        "headline_cheer": (
            "Great job! 🚀" if current["cost"] > previous["cost"] else ""
        ),
        **_metric(1, "Total Requests", current["requests"], previous["requests"], _count),
        **_metric(2, "Total Spend", current["cost"], previous["cost"], _amount),
        **_metric(3, "Voice Minutes Used", current["minutes"], previous["minutes"], _round_quantity),
        **_metric(4, "Agent Interactions", current["agent_requests"], previous["agent_requests"], _count),
        **bars,
        **_insights(current, previous, services),
    }
    report["has_usage"] = current["requests"] > 0
    return report
