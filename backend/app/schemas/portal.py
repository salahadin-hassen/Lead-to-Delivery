"""Client portal response models.

Every field the portal returns is listed here explicitly (``extra="forbid"``),
so internal columns (token hashes, ClickUp IDs, job data, CRM identifiers,
internal tasks or activity) cannot leak by accident.
"""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

InvitationStatus = Literal["pending", "submitted", "expired", "revoked"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortalClient(_Out):
    name: str
    company: str | None


class PortalInvitation(_Out):
    status: InvitationStatus
    service_type: str | None
    template_name: str | None
    deal_name: str | None
    expires_at: datetime
    submitted_at: datetime | None


class PortalMe(_Out):
    client: PortalClient
    invitation: PortalInvitation
    next_step: Literal["onboarding", "project", "contact_agency"]


class OnboardingFormResponse(_Out):
    status: InvitationStatus
    service_type: str | None
    template_name: str | None
    submitted_at: datetime | None
    form_schema: dict[str, Any]
    prefill: dict[str, Any]


class Progress(_Out):
    total_tasks: int
    completed_tasks: int
    open_tasks: int
    percent: int


class TaskView(_Out):
    title: str
    description: str | None
    status: Literal["todo", "in_progress", "done"]
    due_date: date | None
    completed_at: datetime | None


class MilestoneView(_Out):
    name: str
    position: int
    status: Literal["pending", "in_progress", "done"]
    due_date: date | None
    tasks: list[TaskView]


class ActivityView(_Out):
    type: str
    summary: str
    created_at: datetime


class ProjectView(_Out):
    id: UUID
    name: str
    status: Literal["active", "on_hold", "completed", "cancelled"]
    description: str | None
    start_date: date | None
    due_date: date | None
    created_at: datetime
    progress: Progress
    milestones: list[MilestoneView]
    unassigned_tasks: list[TaskView]
    activity: list[ActivityView]


def compute_progress(total: int, completed: int) -> Progress:
    """Completed / total over ALL tasks of the project (internal ones included,
    since they are real work), floored to a whole percent so 100 means every
    task is done. A project with no tasks reports 0 %."""
    percent = (completed * 100) // total if total > 0 else 0
    return Progress(total_tasks=total, completed_tasks=completed, open_tasks=total - completed, percent=percent)
