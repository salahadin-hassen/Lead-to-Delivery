"""Independent integration worker process.

    cd backend && .venv/bin/python -m app.jobs.worker

Runs separately from the API. Loads settings, opens its own connection pool,
builds the handler registry, then repeatedly claims and runs one job; when
nothing is due it sleeps WORKER_POLL_SECONDS. SIGINT/SIGTERM stop the loop
after the current job (a job is never abandoned mid-finalisation); the pool is
closed on exit. Several workers may run at once: claiming uses
FOR UPDATE SKIP LOCKED and every result is fenced.
"""

import logging
import os
import signal
import socket
import sys
import threading
import uuid

from app import db
from app.config import Settings, get_settings
from app.jobs import handlers, runner
from app.jobs.registry import HandlerRegistry

logger = logging.getLogger("app.jobs.worker")


def default_worker_id() -> str:
    host = "".join(c if c.isalnum() or c in ".-" else "-" for c in socket.gethostname())[:40] or "host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def run(settings: Settings, registry: HandlerRegistry, stop: threading.Event, *, worker_id: str | None = None) -> int:
    """Worker loop. Returns the number of jobs processed. Closes the pool on exit."""
    worker_id = worker_id or settings.worker_id or default_worker_id()
    db.configure_pool(settings.database_url.get_secret_value())
    processed = 0
    try:
        logger.info("worker started: id=%s handlers=%s lease=%ss poll=%ss",
                    worker_id, registry.job_types() or "none", settings.worker_lease_seconds,
                    settings.worker_poll_seconds)
        if not len(registry):
            logger.warning("no job handlers registered: queued jobs stay pending until adapters exist")
        while not stop.is_set():
            try:
                report = runner.run_one(registry, worker_id=worker_id, lease_seconds=settings.worker_lease_seconds,
                                        max_retry_delay=settings.worker_max_retry_delay_seconds)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive; the lease recovers the job
                logger.error("worker iteration failed: %s", type(exc).__name__)
                report = None
                stop.wait(settings.worker_poll_seconds)
                continue
            if report is None:
                stop.wait(settings.worker_poll_seconds)
            else:
                processed += 1
    finally:
        registry.close()
        db.close_pool()
        logger.info("worker stopped: id=%s processed=%d", worker_id, processed)
    return processed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stdout)
    settings = get_settings()
    stop = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        logger.info("signal %s received; stopping after the current job", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    run(settings, handlers.build_registry(settings), stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
