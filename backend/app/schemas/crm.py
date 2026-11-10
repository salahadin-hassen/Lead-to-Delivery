"""Normalized CRM ``deal.won`` webhook payload.

This is a generic contract. Payloads from GoHighLevel, HubSpot or any other
CRM must be transformed into this shape before they are sent (for example by
an n8n workflow); the application has no native connection to those products.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CrmSource = Literal["generic", "gohighlevel", "hubspot"]

MAX_DEAL_VALUE = Decimal("1000000000")  # 1e9 major units; keeps cents inside bigint easily


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


# Identifiers coming from a CRM: printable, no control characters, bounded.
ExternalId = Annotated[
    str,
    BeforeValidator(_strip),
    StringConstraints(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
EventId = Annotated[
    str,
    BeforeValidator(_strip),
    StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$"),
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")]
# Constraints sit on the inner str so that None (field omitted) is never pattern-checked.
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Phone = Annotated[str, StringConstraints(max_length=40, pattern=r"^[0-9+()\-. ]{3,40}$")]
TemplateKey = Annotated[
    str, BeforeValidator(_lower), StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
]


class CrmDeal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: ExternalId
    name: Annotated[ShortText | None, BeforeValidator(_blank_to_none)] = None
    value: Annotated[Decimal | None, Field(ge=0, le=MAX_DEAL_VALUE, decimal_places=2)] = None
    currency: Annotated[CurrencyCode | None, BeforeValidator(_upper)] = None
    # Must equal the key of an active project template (checked against the database).
    service_type: TemplateKey

    @field_validator("value", mode="before")
    @classmethod
    def _no_booleans(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("value must be a number")
        return value

    @model_validator(mode="after")
    def _currency_with_value(self) -> "CrmDeal":
        if self.value is not None and self.currency is None:
            raise ValueError("currency is required when value is supplied")
        return self

    @property
    def value_cents(self) -> int | None:
        return None if self.value is None else int(self.value * 100)


class CrmContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: Annotated[ExternalId | None, BeforeValidator(_blank_to_none)] = None
    name: Annotated[str, BeforeValidator(_strip), StringConstraints(min_length=1, max_length=200)]
    # Trimmed and lower-cased before validation, so matching is case-insensitive.
    email: Annotated[EmailStr | None, BeforeValidator(_blank_to_none), BeforeValidator(_lower)] = None
    company: Annotated[ShortText | None, BeforeValidator(_blank_to_none)] = None
    phone: Annotated[Phone | None, BeforeValidator(_blank_to_none)] = None

    @model_validator(mode="after")
    def _needs_stable_identity(self) -> "CrmContact":
        if self.external_id is None and self.email is None:
            raise ValueError("contact requires external_id or email")
        return self


class DealWonEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: EventId
    event_type: Literal["deal.won"]
    occurred_at: AwareDatetime
    source: Annotated[CrmSource, BeforeValidator(_lower)]
    deal: CrmDeal
    contact: CrmContact

    @field_validator("occurred_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    def normalized_payload(self) -> dict[str, Any]:
        """Canonical JSON stored in webhook_events.payload and compared on replay.

        Equivalent inputs (email case, whitespace, time-zone notation, 2500 vs
        2500.00) normalise to the same document.
        """
        data = self.model_dump(mode="json")
        if self.deal.value is not None:
            data["deal"]["value"] = f"{self.deal.value:.2f}"
        return data
