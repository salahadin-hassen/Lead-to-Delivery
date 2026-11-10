"""Job handler registry and the validated context a handler receives."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db import Connection
from app.jobs.result import IntegrationResult


class JobContext(BaseModel):
    """What a handler may use. Built from the claimed row; read-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    job_type: str
    idempotency_key: str
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    attempt_number: int
    max_attempts: int
    worker_id: str
    lease_seconds: int
    # True when the previous attempt's outcome is unknown (lease expired or a
    # reported "unknown"). Adapters MUST reconcile with the external system
    # (look the resource up) before creating anything.
    previous_outcome_unknown: bool


# execute(ctx) runs with NO database connection held and may call external APIs.
Execute = Callable[[JobContext], IntegrationResult]
# on_success(conn, ctx, result) runs inside the fenced finalisation
# transaction, e.g. to store tasks.clickup_task_id. It must only write to
# PostgreSQL; if it raises, nothing of the finalisation is committed.
OnSuccess = Callable[[Connection, JobContext, IntegrationResult], None]


@dataclass(frozen=True)
class Handler:
    job_type: str
    execute: Execute
    on_success: OnSuccess | None = None
    # Optional Pydantic model; an invalid payload is a permanent failure and
    # the handler is not called.
    payload_model: type[BaseModel] | None = None
    # Called once when the worker shuts down (e.g. to close an HTTP client).
    close: Callable[[], None] | None = None


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, handler: Handler) -> None:
        if handler.job_type in self._handlers:
            raise ValueError(f"handler already registered for {handler.job_type}")
        self._handlers[handler.job_type] = handler

    def get(self, job_type: str) -> Handler | None:
        return self._handlers.get(job_type)

    def job_types(self) -> list[str]:
        return sorted(self._handlers)

    def close(self) -> None:
        """Release adapter resources; one failing close never blocks the others."""
        for handler in self._handlers.values():
            if handler.close is not None:
                try:
                    handler.close()
                except Exception:  # noqa: BLE001
                    pass

    def __len__(self) -> int:
        return len(self._handlers)
