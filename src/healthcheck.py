"""Docker healthcheck script for trade-engine.

Pings Redis (reads REDIS_URL from env) and checks engine liveness.
Exits 0 if healthy, 1 if unhealthy.
Usage: python -m healthcheck
"""

import asyncio
import os
import sys

import redis.asyncio as redis


async def _check() -> bool:
    url = os.getenv("ENGINE_REDIS_URL", "redis://redis:6379/0")
    try:
        r = redis.from_url(url)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


def main() -> None:
    ok = asyncio.run(_check())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
