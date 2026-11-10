"""A worker process for the multi-process serialization test.

    python -m tests.proc_worker <database_url> <out_file> <worker_id> <go_file>

Waits until <go_file> exists (so all processes start together), then claims
and runs `fake.op` jobs until none are left, recording each execution interval
(job, coalesce key from the payload, start, end) as JSON lines.
"""

import json
import os
import sys
import time

from app import db
from app.jobs import runner
from app.jobs.registry import Handler, HandlerRegistry
from app.jobs.result import IntegrationResult


def main(url: str, out_file: str, worker_id: str, go_file: str) -> int:
    db.configure_pool(url)
    records = []

    def handler(ctx):
        start = time.monotonic_ns()
        time.sleep(0.2)
        records.append({"job": str(ctx.job_id), "key": ctx.payload.get("key"), "start": start,
                        "end": time.monotonic_ns(), "worker": worker_id, "pid": os.getpid()})
        return IntegrationResult.success()

    registry = HandlerRegistry()
    registry.register(Handler("fake.op", execute=handler))
    deadline = time.monotonic() + 20
    while not os.path.exists(go_file) and time.monotonic() < deadline:
        time.sleep(0.005)
    idle = 0
    try:
        while idle < 15 and time.monotonic() < deadline:
            if runner.run_one(registry, worker_id=worker_id, lease_seconds=60) is None:
                idle += 1
                time.sleep(0.02)
            else:
                idle = 0
    finally:
        db.close_pool()
        with open(out_file, "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:5]))
