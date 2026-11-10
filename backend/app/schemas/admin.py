"""Administrator request bodies (``extra="forbid"`` everywhere)."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, StringConstraints


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _blank_to_none(value: Any) -> Any:
    value = _strip(value)
    return value or None if isinstance(value, str) else value


def _lower_email(value: Any) -> Any:
    value = _blank_to_none(value)
    return value.lower() if isinstance(value, str) else value


Name = Annotated[str, BeforeValidator(_strip), StringConstraints(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]
Company = Annotated[Annotated[str, StringConstraints(max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")] | None,
                    BeforeValidator(_blank_to_none)]
Phone = Annotated[Annotated[str, StringConstraints(pattern=r"^[0-9+()\-. ]{3,40}$")] | None, BeforeValidator(_blank_to_none)]
Email = Annotated[EmailStr | None, BeforeValidator(_lower_email)]
Assignee = Annotated[Annotated[str, StringConstraints(max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")] | None,
                     BeforeValidator(_blank_to_none)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientCreate(_In):
    name: Name
    email: Email = None
    company: Company = None
    phone: Phone = None


class ClientUpdate(_In):
    """Only fields present in the request are changed. CRM identity and status
    cannot be changed here (status: archive/restore endpoints)."""
    name: Name | None = None
    email: Email = None
    company: Company = None
    phone: Phone = None


class ProjectUpdate(_In):
    status: Literal["active", "on_hold", "completed", "cancelled"]


class TaskUpdate(_In):
    status: Literal["todo", "in_progress", "done"] | None = None
    assignee_name: Assignee = None
    assignee_email: Email = None


class JobResolveDelivered(_In):
    # Must be literally true: the administrator saw the message in the channel.
    message_found_in_channel: Literal[True]


class JobRetry(_In):
    additional_attempts: int = Field(default=3, ge=1, le=20)
    reconciliation_confirmed: bool = False
