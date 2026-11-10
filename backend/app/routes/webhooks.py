"""POST /api/webhooks/crm/deal-won: generic, HMAC-signed CRM intake.

Order of checks (each step runs only if the previous one passed):
  1. CRM_WEBHOOK_SECRET configured (>= 32 chars)        else 503
  2. Content-Type application/json                      else 415
  3. Body <= 64 KiB (Content-Length and actual bytes)    else 413
  4. HMAC signature over the RAW body + timestamp window else 401
  5. JSON + schema validation                           else 422
  6. Transactional intake (services.crm_intake)

Nothing is written to the database before step 6. Logs never contain the
secret, signatures, tokens, onboarding URLs or payload contents.
"""

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.errors import (
    EventIdReusedError,
    IntakeRetryExhaustedError,
    UnknownServiceTypeError,
    WebhookAuthError,
)
from app.routes._http import BodyTooLarge, is_json, read_body, validation_errors
from app.routes._http import error as _error
from app.schemas.crm import DealWonEvent
from app.security import verify_webhook_signature
from app.services import crm_intake

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BODY_BYTES = 64 * 1024
TIMESTAMP_HEADER = "x-ltd-timestamp"
SIGNATURE_HEADER = "x-ltd-signature"

_AUTH_MESSAGES = {
    "missing_signature": "Missing X-LTD-Timestamp or X-LTD-Signature header.",
    "malformed_timestamp": "X-LTD-Timestamp must be Unix time in seconds.",
    "timestamp_out_of_window": "X-LTD-Timestamp is outside the allowed 5-minute window.",
    "malformed_signature": "X-LTD-Signature must be 'sha256=' followed by 64 hex characters.",
    "invalid_signature": "Signature does not match.",
}


def _now() -> float:
    return time.time()


@router.post("/webhooks/crm/deal-won")
async def crm_deal_won(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    secret = settings.usable_webhook_secret()
    if secret is None:
        logger.error("crm webhook rejected: CRM_WEBHOOK_SECRET is not configured")
        return _error(503, "webhook_not_configured", "CRM webhook is not configured.")

    if not is_json(request):
        return _error(415, "unsupported_media_type", "Content-Type must be application/json.")

    try:
        body = await read_body(request, MAX_BODY_BYTES)
    except BodyTooLarge:
        return _error(413, "payload_too_large", f"Request body exceeds {MAX_BODY_BYTES} bytes.")

    try:
        verify_webhook_signature(
            secret=secret,
            timestamp=request.headers.get(TIMESTAMP_HEADER),
            signature=request.headers.get(SIGNATURE_HEADER),
            body=body,
            now=_now(),
        )
    except WebhookAuthError as exc:
        logger.warning("crm webhook authentication failed: %s", exc.code)
        return _error(401, exc.code, _AUTH_MESSAGES[exc.code])

    try:
        event = DealWonEvent.model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": validation_errors(exc)})

    try:
        result = await run_in_threadpool(
            crm_intake.process_deal_won, event, app_base_url=settings.app_base_url
        )
    except EventIdReusedError:
        logger.warning("crm webhook %s/%s: event id reused with a different payload",
                       event.source, event.event_id)
        return _error(409, "event_id_reused",
                      "This event_id was already received with a different payload.",
                      event_id=event.event_id)
    except UnknownServiceTypeError:
        return JSONResponse(status_code=422, content={"detail": [{
            "loc": ["body", "deal", "service_type"],
            "msg": "service_type does not match an active project template",
            "type": "unknown_service_type",
        }]})
    except IntakeRetryExhaustedError:
        logger.warning("crm webhook %s/%s: retries exhausted", event.source, event.event_id)
        return _error(503, "retry_later", "Concurrent update in progress; retry the delivery.")

    logger.info("crm webhook %s/%s: %s", event.source, event.event_id, result.status)
    if result.status == "created":
        return JSONResponse(status_code=201, content={
            "status": "created",
            "client_id": str(result.client_id),
            "invitation_id": str(result.invitation_id),
            "onboarding_url": result.onboarding_url,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
        })
    if result.status in ("duplicate", "duplicate_deal"):
        return JSONResponse(status_code=200, content={"status": result.status, "event_id": result.event_id})
    return _error(409, result.conflict_code or "identity_conflict",
                  result.conflict_message or "Client identity conflict.", event_id=result.event_id)

