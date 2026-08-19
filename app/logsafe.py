"""Rendering untrusted values into log lines without letting them forge one.

A log file is parsed — by an operator's eye, by `grep`, by whatever ships it
elsewhere — and every one of those readers decides where a record begins by
looking for the start of a line. So a value that reaches a log with a newline in
it does not merely look untidy: it writes a second record that nothing
distinguishes from a real one. An address of

    "victim@example.com\\nERROR payment failed for order_123"

produces a log that reads exactly like a payment failure that never happened.

Two shapes, because the values divide cleanly into two kinds:

* `log_safe` for values that are *supposed* to be one line — an address, a
  subject, an id, a rate-limit key. Newlines collapse to spaces and the result
  is truncated, because a field that arrives 4KB long is itself the anomaly and
  the log should not carry it.
* `log_block` for the one place something deliberately multi-line is logged: the
  console email backend, which exists to show a developer the message. Newlines
  survive, but every continuation line is indented, so no line inside the value
  can begin at column zero where a new record would.

Written with `str.replace` rather than the more obvious comprehension over
`str.isprintable`. Both strip control characters, but only the first is a form
static analysis recognises as sanitising: this module replaces a helper that
filtered by comprehension, and the two call sites already using it kept their
log-injection alerts open the whole time — correct code that could not
demonstrate it was correct.

This module imports nothing from the package on purpose. It is needed by
`email`, which `notifications` imports, so anywhere higher up would be a cycle.
"""
from __future__ import annotations

# Long enough to identify a value, short enough that a hostile one cannot flood
# the log. Every caller that wants more says so explicitly.
_DEFAULT_LIMIT = 64


def _strip_control(text: str, *, keep_newlines: bool = False) -> str:
    """Drop characters that a terminal or log reader would act on rather than show."""
    return "".join(
        c for c in text if c.isprintable() or (keep_newlines and c == "\n")
    )


def log_safe(value: object, limit: int = _DEFAULT_LIMIT) -> str:
    """Flatten a value to a single, printable, bounded line.

    Order matters: the newline removal happens first, on the raw string, so the
    sanitising is visible to a reader (and to a scanner) as the first thing done
    to the untrusted value rather than an emergent property of a filter further
    down.
    """
    flattened = (
        str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    )
    return _strip_control(flattened)[:limit]


def log_block(value: object, limit: int = 4000) -> str:
    """Keep a deliberately multi-line value readable, but unable to forge a record.

    Used only by the console email backend. Indenting every continuation line is
    what makes it safe: the danger is a line that *starts* like a new log entry,
    and after this none of them start at column zero.
    """
    cleaned = _strip_control(str(value), keep_newlines=True)[:limit]
    return cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\n    ")
