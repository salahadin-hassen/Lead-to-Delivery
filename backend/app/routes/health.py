import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db
from app.store import heartbeats

logger = logging.getLogger(__name__)

router = APIRouter()


def _unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/health")
def health(request: Request) -> JSONResponse:
    """200 only when PostgreSQL answers AND the schema is fully migrated.

    The response body is deliberately generic; the reason is logged without
    connection details.
    """
    expected: int = request.app.state.expected_schema_version
    try:
        version = db.applied_schema_version()
    except Exception as exc:  # noqa: BLE001 - a health probe must answer 503, never 500
        logger.warning("health: database unreachable (%s)", type(exc).__name__)
        return _unavailable()
    if version is None or version < expected:
        logger.warning(
            "health: schema at version %s, code expects %s; run `python -m app.migrations`",
            version, expected,
        )
        return _unavailable()
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/health/worker")
def worker_health() -> JSONResponse:
    """200 when at least one integration worker reported in the last 90 seconds.

    Workers are a separate service; point the platform's monitoring (not its
    API health check) at this endpoint.
    """
    try:
        with db.connection() as conn:
            alive = heartbeats.live_workers(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker health: database unreachable (%s)", type(exc).__name__)
        return _unavailable()
    if alive < 1:
        logger.warning("worker health: no live integration worker")
        return JSONResponse(status_code=503, content={"status": "no_worker"})
    return JSONResponse(status_code=200, content={"status": "ok"})
