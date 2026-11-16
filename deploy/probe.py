"""Read-only probes, executed in the target Docker network or on the native host."""

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from config_probes import backend_checks, redis_check
from storage import read


async def probe(document, source):
    env = document["backend"]["environment"]
    results = await backend_checks(
        env["DATABASE_URL"], env["REDIS_URL"], env["CELERY_BROKER_URL"], Path(source)
    )
    results.append(await redis_check("Celery 结果 Redis", env["CELERY_RESULT_BACKEND_URL"]))
    return [asdict(result) for result in results]


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe(read(sys.argv[1]), sys.argv[2])), ensure_ascii=False))
