"""Client portal API. Every endpoint requires ``Authorization: Bearer <token>``.

The token is resolved server-side to (client, invitation); nothing in the
request (path, query or body) can widen that scope. All responses carry
``Cache-Control: no-store``. The Authorization header and token are never logged.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from app.errors import (
    AlreadySubmittedError,
    InvitationClosedError,
    PortalAuthError,
    ProjectNotFoundError,
    TemplateUnavailableError,
)
from app.routes._http import BodyTooLarge, is_json, read_body, validation_errors
from app.routes._http import error as _error
from app.schemas.onboarding import form_for
from app.services import onboarding as onboarding_service
from app.services import portal
from app.services.portal import PortalContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portal")

MAX_BODY_BYTES = 32 * 1024
NO_STORE = {"Cache-Control": "no-store"}

AUTH_MESSAGES = {
    "token_missing": "Authorization: Bearer <token> is required.",
    "token_invalid": "This access link is not valid.",
    "token_expired": "This access link has expired. Please ask the agency for a new one.",
    "token_revoked": "This access link is no longer active. Please ask the agency for a new one.",
}


def portal_auth_response(exc: PortalAuthError) -> JSONResponse:
    return _error(401, exc.code, AUTH_MESSAGES.get(exc.code, AUTH_MESSAGES["token_invalid"]),
                  headers={"WWW-Authenticate": "Bearer", **NO_STORE})


def portal_context(request: Request) -> PortalContext:
    """FastAPI dependency: the trusted (client, invitation) behind the bearer token."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise PortalAuthError("token_missing")
    return portal.authenticate(token.strip())


def _ok(model: BaseModel, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=model.model_dump(mode="json"), headers=NO_STORE)


def _not_found() -> JSONResponse:
    return _error(404, "project_not_found", "No project is available for this access link yet.",
                  headers=NO_STORE)


@router.get("/me")
def get_me(ctx: PortalContext = Depends(portal_context)) -> JSONResponse:
    return _ok(portal.me(ctx))


@router.get("/onboarding")
def get_onboarding(ctx: PortalContext = Depends(portal_context)) -> JSONResponse:
    return _ok(portal.onboarding_form(ctx))


@router.post("/onboarding")
async def post_onboarding(request: Request, ctx: PortalContext = Depends(portal_context)) -> JSONResponse:
    if not is_json(request):
        return _error(415, "unsupported_media_type", "Content-Type must be application/json.", headers=NO_STORE)
    try:
        body = await read_body(request, MAX_BODY_BYTES)
    except BodyTooLarge:
        return _error(413, "payload_too_large", f"Request body exceeds {MAX_BODY_BYTES} bytes.", headers=NO_STORE)

    # The form model is chosen from the invitation's service_type, never from the request.
    try:
        form = form_for(ctx.service_type).model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": validation_errors(exc)}, headers=NO_STORE)

    settings = request.app.state.settings
    try:
        result = await run_in_threadpool(
            onboarding_service.submit, ctx, form, max_attempts=settings.job_max_attempts
        )
    except AlreadySubmittedError:
        return _error(409, "already_submitted", "Onboarding has already been submitted for this project.",
                      headers=NO_STORE)
    except InvitationClosedError as exc:
        return _error(409, exc.code, "This onboarding invitation is no longer open.", headers=NO_STORE)
    except TemplateUnavailableError:
        logger.error("onboarding submission blocked: project template unavailable")
        return _error(503, "template_unavailable", "Project setup is temporarily unavailable.", headers=NO_STORE)

    view = await run_in_threadpool(portal.project_view, ctx, result.project_id)
    return JSONResponse(status_code=201, content={"status": "submitted", "project": view.model_dump(mode="json")},
                        headers=NO_STORE)


@router.get("/project")
def get_project(ctx: PortalContext = Depends(portal_context)) -> JSONResponse:
    try:
        return _ok(portal.project_view(ctx))
    except ProjectNotFoundError:
        return _not_found()


@router.get("/projects/{project_id}")
def get_project_by_id(project_id: str, ctx: PortalContext = Depends(portal_context)) -> JSONResponse:
    """Same as /project, for deep links. Any id other than this invitation's
    own project (another client's, or another invitation's) is a 404."""
    try:
        requested = UUID(project_id)
    except ValueError:
        return _not_found()
    try:
        return _ok(portal.project_view(ctx, requested))
    except ProjectNotFoundError:
        return _not_found()
