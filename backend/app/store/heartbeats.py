"""SQL for worker liveness (worker_heartbeats)."""

from app.db import Connection

ALIVE_WITHIN_SECONDS = 90


def beat(conn: Connection, worker_id: str, processed: int) -> None:
    conn.execute(
        "INSERT INTO worker_heartbeats (worker_id, jobs_processed) VALUES (%s, %s) "
        "ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now(), jobs_processed = EXCLUDED.jobs_processed, "
        "stopped_at = NULL",
        (worker_id, processed),
    )


def stopped(conn: Connection, worker_id: str, processed: int) -> None:
    conn.execute("UPDATE worker_heartbeats SET stopped_at = now(), last_seen_at = now(), jobs_processed = %s "
                 "WHERE worker_id = %s", (processed, worker_id))


def prune(conn: Connection, days: int = 7) -> int:
    return conn.execute("DELETE FROM worker_heartbeats WHERE last_seen_at < now() - make_interval(days => %s)",
                        (days,)).rowcount


def live_workers(conn: Connection, within_seconds: int = ALIVE_WITHIN_SECONDS) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM worker_heartbeats WHERE stopped_at IS NULL "
        "AND last_seen_at > now() - make_interval(secs => %s)", (within_seconds,)
    ).fetchone()["n"]
