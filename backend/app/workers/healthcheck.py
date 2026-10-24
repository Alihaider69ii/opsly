"""Exit status 0 if this worker is alive, 1 if not. For a container HEALTHCHECK:

    HEALTHCHECK CMD python -m app.workers.healthcheck

``--cluster`` instead asks Redis whether ANY worker has beaten recently (for a monitoring job that
is not inside the worker's container). Prints a word, never an address or a credential.
"""

import argparse
import sys
from collections.abc import Sequence

from app.core.config import get_settings
from app.workers.heartbeat import REDIS_TTL_SECONDS, file_is_fresh, heartbeat_file
from app.workers.queue import RedisJobQueue

MAX_AGE_SECONDS = 30.0  # six missed beats


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cluster", action="store_true", help="ask Redis about any worker")
    args = parser.parse_args(argv)
    if args.cluster:
        settings = get_settings()
        queue = RedisJobQueue(settings.redis_url, key=settings.redis_queue_key, socket_timeout=2.0)
        try:
            alive = queue.live_workers() > 0
        except Exception:  # noqa: BLE001 - any failure to ask means "cannot confirm"
            alive = False
        finally:
            queue.close()
        print("ok" if alive else f"no worker beat in the last {REDIS_TTL_SECONDS}s")
        return 0 if alive else 1
    alive = file_is_fresh(heartbeat_file(), MAX_AGE_SECONDS)
    print("ok" if alive else "stale")
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
