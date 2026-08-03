"""Time bucketing for the usage charts.

The dashboard plots spend, requests and quantity over time at Daily / Hourly /
Minute granularity across a date range. Two things make that more than a
`$group`:

* **Gaps must be filled.** A day with no usage still needs a zero point, or the
  chart's x-axis silently skips it and a quiet Tuesday looks like it never
  happened. So the full range of buckets is generated here and the aggregation
  results are merged onto it.
* **Ranges must be bounded.** A minute-granularity query over a year is 525,600
  buckets; nothing good comes of building that response. Each granularity caps
  the span it will serve and says so.

Bucket labels come from Mongo's ``$dateToString`` and are reproduced here with
the *same* format strings, so the generated and aggregated keys line up. All
bucketing is in UTC — the timestamps are stored in UTC, and re-bucketing by a
viewer's local zone would make two people disagree about the same invoice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Granularity:
    key: str
    # Shared by Mongo's $dateToString and Python's strftime, so the keys match.
    fmt: str
    step: timedelta
    max_span: timedelta
    default_span: timedelta


GRANULARITIES: dict[str, Granularity] = {
    g.key: g
    for g in [
        Granularity("daily", "%Y-%m-%d", timedelta(days=1), timedelta(days=731), timedelta(days=30)),
        Granularity("hourly", "%Y-%m-%dT%H:00", timedelta(hours=1), timedelta(days=62), timedelta(days=2)),
        Granularity("minute", "%Y-%m-%dT%H:%M", timedelta(minutes=1), timedelta(days=2), timedelta(hours=6)),
    ]
}


class RangeTooLargeError(ValueError):
    pass


def get_granularity(key: str) -> Granularity:
    granularity = GRANULARITIES.get((key or "daily").lower())
    if granularity is None:
        raise ValueError(
            f"Unknown granularity '{key}'. Valid: {', '.join(GRANULARITIES)}"
        )
    return granularity


def parse_instant(value: str) -> datetime:
    """Parse an ISO date or datetime as UTC.

    Accepts "2026-04-12" as well as a full timestamp, because the date pickers
    send the former. A value with no zone is read as UTC rather than local, so
    the same request means the same thing wherever it is issued from.
    """
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def truncate(moment: datetime, granularity: Granularity) -> datetime:
    """Snap an instant down to the start of its bucket."""
    if granularity.key == "daily":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity.key == "hourly":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(second=0, microsecond=0)


def resolve_range(
    granularity: Granularity,
    start: str | None,
    end: str | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Work out the window to report, defaulting and validating it.

    ``to`` defaults to now and ``from`` to one default span before it. The end
    is exclusive of nothing — the bucket containing ``to`` is included.
    """
    finish = parse_instant(end) if end else (now or datetime.now(timezone.utc))
    begin = parse_instant(start) if start else finish - granularity.default_span

    if begin > finish:
        raise ValueError("'from' must not be after 'to'.")

    span = finish - begin
    if span > granularity.max_span:
        raise RangeTooLargeError(
            f"A {granularity.key} range may span at most "
            f"{granularity.max_span.days or 2} days; got {span.days} days. "
            "Narrow the range or use a coarser granularity."
        )
    return begin, finish


def bucket_labels(
    begin: datetime, finish: datetime, granularity: Granularity
) -> list[str]:
    """Every bucket label in the range, including the empty ones."""
    labels: list[str] = []
    cursor = truncate(begin, granularity)
    last = truncate(finish, granularity)
    while cursor <= last:
        labels.append(cursor.strftime(granularity.fmt))
        cursor += granularity.step
    return labels
