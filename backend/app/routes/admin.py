"""Administrator management API. Every route requires the admin session cookie;
state-changing routes also pass the CSRF checks (routes/admin_deps.py)."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app import db
from app.errors import NotFoundError
from app.routes.admin_deps import csrf_protect, require_admin
from app.schemas.admin import ClientCreate, ClientUpdate, ProjectUpdate, TaskUpdate
from app.schemas.portal import compute_progress
from app.services import admin_ops
from app.store import admin_queries as q

router = APIRouter(prefix="/admin", dependencies=[Depends(csrf_protect), Depends(require_admin)])

NO_STORE = {"Cache-Control": "no-store"}
Limit = Query(default=25, ge=1, le=100)
Offset = Query(default=0, ge=0, le=100_000)
Search = Query(default=None, min_length=1, max_length=100, alias="q")  # ?q=…


def ok(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=jsonable_encoder(payload), headers=NO_STORE)


def page(items: list[dict], total: int, limit: int, offset: int) -> JSONResponse:
    return ok({"items": items, "total": total, "limit": limit, "offset": offset})


def _with_progress(row: dict) -> dict:
    row["progress"] = compute_progress(row.pop("total_tasks"), row.pop("completed_tasks")).model_dump()
    return row


# ----------------------------------------------------------------- dashboard
@router.get("/dashboard")
def dashboard() -> JSONResponse:
    with db.connection() as conn:
        data = q.dashboard(conn)
    data["active_projects"] = [_with_progress(r) for r in data["active_projects"]]
    return ok(data)


# ------------------------------------------------------------------- clients
@router.get("/clients")
def list_clients(q_: str | None = Search, status: Literal["active", "archived"] | None = None,
                 sort: Literal["created_desc", "created_asc"] = "created_desc",
                 limit: int = Limit, offset: int = Offset) -> JSONResponse:
    with db.connection() as conn:
        items, total = q.list_clients(conn, q=q_, status=status, sort=sort, limit=limit, offset=offset)
    return page(items, total, limit, offset)



@router.post("/clients")
def create_client(body: ClientCreate) -> JSONResponse:
    return ok(admin_ops.create_client(body), status=201)


@router.get("/clients/{client_id}")
def get_client(client_id: UUID) -> JSONResponse:
    with db.connection() as conn:
        client = q.get_client(conn, client_id)
    if client is None:
        raise NotFoundError()
    client["projects"] = [_with_progress(p) for p in client["projects"]]
    return ok(client)


@router.patch("/clients/{client_id}")
def update_client(client_id: UUID, body: ClientUpdate) -> JSONResponse:
    return ok(admin_ops.update_client(client_id, body))


@router.post("/clients/{client_id}/archive")
def archive_client(client_id: UUID) -> JSONResponse:
    return ok(admin_ops.set_client_status(client_id, "archived"))


@router.post("/clients/{client_id}/restore")
def restore_client(client_id: UUID) -> JSONResponse:
    return ok(admin_ops.set_client_status(client_id, "active"))


@router.post("/invitations/{invitation_id}/regenerate-access-link")
def regenerate_access_link(invitation_id: UUID, request: Request) -> JSONResponse:
    url, expires_at, kind = admin_ops.regenerate_access_link(
        invitation_id, app_base_url=request.app.state.settings.app_base_url)
    # Shown once to the administrator; never stored or logged.
    return ok({"access_url": url, "expires_at": expires_at, "kind": kind}, status=201)


# ------------------------------------------------------------------ projects
@router.get("/projects")
def list_projects(q_: str | None = Search,
                  status: Literal["active", "on_hold", "completed", "cancelled"] | None = None,
                  client_id: UUID | None = None, limit: int = Limit, offset: int = Offset) -> JSONResponse:
    with db.connection() as conn:
        items, total = q.list_projects(conn, q=q_, status=status, client_id=client_id, limit=limit, offset=offset)
    return page([_with_progress(r) for r in items], total, limit, offset)


@router.get("/projects/{project_id}")
def get_project(project_id: UUID) -> JSONResponse:
    with db.connection() as conn:
        project = q.get_project(conn, project_id)
    if project is None:
        raise NotFoundError()
    return ok(_with_progress(project))


@router.patch("/projects/{project_id}")
def update_project(project_id: UUID, body: ProjectUpdate, request: Request) -> JSONResponse:
    changed = admin_ops.update_project(project_id, body, max_attempts=request.app.state.settings.job_max_attempts)
    return get_project_response(project_id, changed)


@router.patch("/projects/{project_id}/tasks/{task_id}")
def update_task(project_id: UUID, task_id: UUID, body: TaskUpdate, request: Request) -> JSONResponse:
    changed = admin_ops.update_task(project_id, task_id, body,
                                    max_attempts=request.app.state.settings.job_max_attempts)
    return get_project_response(project_id, changed)


def get_project_response(project_id: UUID, changed: bool) -> JSONResponse:
    with db.connection() as conn:
        project = q.get_project(conn, project_id)
    if project is None:
        raise NotFoundError()
    return ok({"changed": changed, "project": _with_progress(project)})


# ------------------------------------------------------- activity & webhooks
@router.get("/activity")
def list_activity(client_id: UUID | None = None, project_id: UUID | None = None,
                  event_prefix: str | None = Query(default=None, max_length=64, pattern=r"^[a-z0-9_.]+$"),
                  limit: int = Limit, offset: int = Offset) -> JSONResponse:
    with db.connection() as conn:
        items, total = q.list_activity(conn, client_id=client_id, project_id=project_id,
                                       event_prefix=event_prefix, limit=limit, offset=offset)
    return page(items, total, limit, offset)


@router.get("/webhook-events")
def list_webhook_events(status: Literal["received", "processed", "rejected", "failed"] | None = None,
                        source: str | None = Query(default=None, max_length=40, pattern=r"^[a-z0-9_-]+$"),
                        limit: int = Limit, offset: int = Offset) -> JSONResponse:
    with db.connection() as conn:
        items, total = q.list_webhook_events(conn, status=status, source=source, limit=limit, offset=offset)
    return page(items, total, limit, offset)

