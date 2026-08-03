"""Set every system's status by hand.

For when the truth is known but nothing reports it yet — a fresh deployment
where the AI services do not send heartbeats and no probe URLs are configured.
Without this, the status page correctly reads `unknown` for everything.

    python -m scripts.set_status                     # all systems operational
    python -m scripts.set_status down "DB failover"  # all systems down
    python -m scripts.set_status --service tts degraded "Slow synthesis"

Writes straight to Mongo using the app's own database layer, so it needs
MONGODB_URI but no running server and no STATUS_WEBHOOK_SECRET.
"""
from __future__ import annotations

import argparse
import asyncio

from app import database
from app import status as service_status


async def main_async(service: str | None, state: str, detail: str | None) -> int:
    await database.connect()
    try:
        keys = [service] if service else list(service_status.SYSTEMS)
        for key in keys:
            record = await service_status.record(
                key, state, service_status.SOURCE_MANUAL, detail
            )
            print(f"  {service_status.get_system(key).name:<30} -> {record['status']}")
        print(
            f"\nSet {len(keys)} system(s) to {state!r}. Manual statuses do not "
            "expire — they stand until changed."
        )
    finally:
        await database.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Set service status by hand.")
    parser.add_argument(
        "state",
        nargs="?",
        default=service_status.OPERATIONAL,
        choices=service_status.STATUSES,
    )
    parser.add_argument("detail", nargs="?", default=None)
    parser.add_argument(
        "--service",
        default=None,
        choices=sorted(service_status.SYSTEMS),
        help="Only this system (default: all).",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.service, args.state, args.detail))


if __name__ == "__main__":
    raise SystemExit(main())
