"""HTML email templates — loading, rendering, and the values every one shares.

The designed emails live as `.html` files under ``templates/emails``. They are
the files the design hands over, edited only to turn the sample data in them
into ``{{placeholders}}``; keeping them as files rather than strings in Python
means a designer can re-export one without touching code.

Rendering is a deliberately small subset of Mustache, implemented here rather
than pulled in as a dependency:

* ``{{key}}``            — the value, HTML-escaped;
* ``{{&key}}``           — the value, inserted raw (only for markup we build);
* ``{{#key}}…{{/key}}``  — a list repeats the block once per item, a truthy
                           scalar renders it once, a falsy one skips it;
* ``{{^key}}…{{/key}}``  — the inverse, for "nothing to show here" copy;
* ``{{.}}``              — the current item, inside a list of plain strings.

Escaping is on by default because these templates interpolate customer-supplied
text — a display name, a company, an announcement body. A name containing
``<script>`` must render as characters, not markup.

**A missing value raises.** Every caller in this app builds its own context, so
an absent key is a bug in that caller, and blanking it silently would ship an
email with a hole in it (or worse, a reset button with an empty ``href``).
`email.py` catches the error and falls back to the plain-text part, so a
template bug degrades the message rather than losing it.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import get_settings

logger = logging.getLogger("chatbucket_b2b.emailtemplates")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "emails"

_TOKEN = re.compile(r"{{\s*(?P<sigil>[#/^&]?)\s*(?P<key>[A-Za-z0-9_.]+)\s*}}")

# Marks "no value here" as distinct from a legitimate None the caller passed.
_MISSING = object()


class TemplateError(Exception):
    """The template itself is malformed (unbalanced sections)."""


class MissingValueError(KeyError):
    """The context did not carry a key the template asks for."""


# --- Parsing ---------------------------------------------------------------
# A template is parsed once into a tree of ("text" | "var" | "section") nodes
# and cached. Rendering then walks the tree, so the regex runs once per file
# rather than once per email.

def _parse(text: str) -> list:
    root: list = []
    stack: list[list] = [root]
    open_keys: list[str] = []
    pos = 0

    for match in _TOKEN.finditer(text):
        literal = text[pos : match.start()]
        if literal:
            stack[-1].append(("text", literal))
        pos = match.end()

        sigil, key = match.group("sigil"), match.group("key")
        if sigil in ("#", "^"):
            children: list = []
            stack[-1].append(("section", key, children, sigil == "^"))
            stack.append(children)
            open_keys.append(key)
        elif sigil == "/":
            if not open_keys or open_keys[-1] != key:
                raise TemplateError(
                    f"{{{{/{key}}}}} closes "
                    + (f"{{{{#{open_keys[-1]}}}}}" if open_keys else "nothing")
                )
            open_keys.pop()
            stack.pop()
        else:
            stack[-1].append(("var", key, sigil == "&"))

    if open_keys:
        raise TemplateError(f"unclosed section {{{{#{open_keys[-1]}}}}}")
    if pos < len(text):
        root.append(("text", text[pos:]))
    return root


@cache
def _tree(name: str) -> tuple:
    """Parse a template file. Cached: templates never change at runtime."""
    path = TEMPLATE_DIR / f"{name}.html"
    if not path.is_file():
        raise FileNotFoundError(f"No email template named {name!r} in {TEMPLATE_DIR}")
    return tuple(_parse(path.read_text(encoding="utf-8")))


# --- Rendering -------------------------------------------------------------

def _lookup(key: str, stack: list[dict]):
    """Resolve a key against the context stack, innermost frame first."""
    if key == ".":
        return stack[-1].get(".", _MISSING)
    for frame in reversed(stack):
        if isinstance(frame, dict) and key in frame:
            return frame[key]
    return _MISSING


def _stringify(value) -> str:
    if value is None or value is True or value is False:
        # A bare bool or None in a text slot is almost always a context bug,
        # and "True" is never what the design meant to show.
        return ""
    return str(value)


def _render_nodes(nodes, stack: list[dict], out: list[str], where: str) -> None:
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
            continue

        if kind == "var":
            _, key, raw = node
            value = _lookup(key, stack)
            if value is _MISSING:
                raise MissingValueError(f"{where}: no value for {{{{{key}}}}}")
            text = _stringify(value)
            out.append(text if raw else html.escape(text, quote=True))
            continue

        _, key, children, inverted = node
        value = _lookup(key, stack)
        if value is _MISSING:
            raise MissingValueError(f"{where}: no value for section {{{{#{key}}}}}")

        if inverted:
            if not value:
                _render_nodes(children, stack, out, where)
            continue

        if isinstance(value, list | tuple):
            for item in value:
                frame = dict(item) if isinstance(item, dict) else {}
                # `{{.}}` inside a list of plain strings.
                frame["."] = item
                stack.append(frame)
                _render_nodes(children, stack, out, where)
                stack.pop()
        elif isinstance(value, dict):
            stack.append(value)
            _render_nodes(children, stack, out, where)
            stack.pop()
        elif value:
            _render_nodes(children, stack, out, where)


def render(name: str, context: dict) -> str:
    """Render ``templates/emails/<name>.html`` with `context` over the defaults."""
    merged = {**base_context(), **context}
    out: list[str] = []
    _render_nodes(_tree(name), [merged], out, name)
    return "".join(out)


# --- Shared values ---------------------------------------------------------

# Symbols for the currencies this service can be configured with. The code is
# shown for anything else — better a correct "USD 40.00" than a wrong glyph.
_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}
_CURRENCY_NAMES = {
    "INR": "Indian rupees",
    "USD": "US dollars",
    "EUR": "Euros",
    "GBP": "Pounds sterling",
}


def currency_symbol() -> str:
    code = get_settings().currency.upper()
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def currency_name() -> str:
    code = get_settings().currency.upper()
    return _CURRENCY_NAMES.get(code, code)


# --- Display formatting ----------------------------------------------------
# Everything is stored in UTC; a receipt has to read in the customer's clock.

def _local(moment: datetime) -> datetime:
    settings = get_settings()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.astimezone(ZoneInfo(settings.display_timezone))
    except ZoneInfoNotFoundError:
        # A typo in DISPLAY_TIMEZONE must not stop a receipt going out; UTC is
        # wrong by hours, a crash is wrong entirely.
        logger.error("unknown DISPLAY_TIMEZONE %r; showing UTC", settings.display_timezone)
        return moment.astimezone(timezone.utc)


def local_now() -> datetime:
    """Now, in the configured display zone.

    The scheduler decides what is due from this rather than from UTC: "send the
    monthly report on the 1st at 6am" means the customer's 1st and the
    customer's 6am, and in IST those are five and a half hours from the
    server's.
    """
    return _local(datetime.now(timezone.utc))


def fmt_date(moment: datetime) -> str:
    """``31 July 2026`` — no leading zero, which strftime cannot do portably."""
    local = _local(moment)
    return f"{local.day:02d} {local.strftime('%B %Y')}"


def fmt_short_date(moment: datetime) -> str:
    """``06 AUG 2026``, the compact form the maintenance card uses."""
    local = _local(moment)
    return f"{local.day:02d} {local.strftime('%b %Y').upper()}"


def fmt_time(moment: datetime) -> str:
    """``6:59 PM IST`` in the configured display zone."""
    local = _local(moment)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.strftime('%M %p')} {local.tzname()}"


def fmt_month(moment: datetime) -> str:
    """``Jul 2026`` — the label on the month-over-month comparison."""
    return _local(moment).strftime("%b %Y")


def base_context() -> dict:
    """The header, footer and branding values every template asks for.

    Built per render rather than cached, so `{{year}}` is still right after a
    process has been running across New Year.
    """
    settings = get_settings()
    return {
        "year": datetime.now(timezone.utc).year,
        "support_email": settings.support_email,
        "visit_url": settings.marketing_url,
        "track_url": settings.track_query_url_for,
        "dashboard_url": settings.dashboard_url_for,
        "login_url": settings.login_url_for,
        "privacy_policy_url": settings.privacy_policy_url_for,
        "terms_url": settings.terms_url_for,
        "currency_symbol": currency_symbol(),
        "currency_name": currency_name(),
    }
