"""Client onboarding form: shared core questions plus a short, service-specific
section chosen by the SERVER from the invitation's service_type.

The client never chooses the service type, statuses, assignees, IDs or any
integration setting: unknown fields are rejected at every level.
The same models produce the JSON Schema returned by GET /api/portal/onboarding,
so the form definition and the validation can never drift apart.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

MAX_DELIVERY_HORIZON_DAYS = 730

# Printable text; newlines and tabs allowed in long answers, other control
# characters rejected.
_LINE = r"^[^\x00-\x1f\x7f]*$"
_MULTILINE = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*$"


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return value


def _blank_to_none(value: Any) -> Any:
    value = _clean(value)
    return value or None if isinstance(value, str) else value


def _line(min_length: int, max_length: int):
    return Annotated[str, BeforeValidator(_clean),
                     StringConstraints(min_length=min_length, max_length=max_length, pattern=_LINE)]


def _text(min_length: int, max_length: int):
    return Annotated[str, BeforeValidator(_clean),
                     StringConstraints(min_length=min_length, max_length=max_length, pattern=_MULTILINE)]


def _optional_text(max_length: int):
    inner = Annotated[str, StringConstraints(min_length=1, max_length=max_length, pattern=_MULTILINE)]
    return Annotated[inner | None, BeforeValidator(_blank_to_none)]


ShortItem = Annotated[str, BeforeValidator(_clean), StringConstraints(min_length=1, max_length=100, pattern=_LINE)]


def _future_delivery_date(value: date | None) -> date | None:
    if value is None:
        return value
    today = datetime.now(UTC).date()
    if value <= today:
        raise ValueError("desired_delivery_date must be in the future")
    if value > today + timedelta(days=MAX_DELIVERY_HORIZON_DAYS):
        raise ValueError(f"desired_delivery_date must be within {MAX_DELIVERY_HORIZON_DAYS} days")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class CommunicationPreferences(_Strict):
    preferred_channel: Literal["email", "phone", "video_call", "slack"] = Field(
        title="Preferred channel")
    update_frequency: Literal["weekly", "biweekly", "monthly"] = Field(
        default="weekly", title="How often would you like progress updates?")
    contact_email: Annotated[EmailStr | None, BeforeValidator(_blank_to_none)] = Field(
        default=None, title="Contact email for project updates (if different)")

    @field_validator("contact_email", mode="before")
    @classmethod
    def _lower_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


# ------------------------------------------------- service-specific sections
class WebsiteBuildDetails(_Strict):
    existing_website_url: HttpUrl | None = Field(default=None, title="Current website (if any)")
    estimated_page_count: int | None = Field(default=None, ge=1, le=200, title="Roughly how many pages?")
    cms_preference: Literal["wordpress", "webflow", "shopify", "no_preference", "other"] = Field(
        default="no_preference", title="Preferred CMS")
    has_brand_assets: bool = Field(title="Do you have a logo and brand guidelines ready?")


class SeoRetainerDetails(_Strict):
    website_url: HttpUrl = Field(title="Website to optimise")
    target_locations: list[ShortItem] = Field(default_factory=list, max_length=10,
                                              title="Target locations (cities, regions or countries)")
    priority_keywords: list[ShortItem] = Field(default_factory=list, max_length=25,
                                               title="Priority keywords or topics")
    has_analytics_access: bool = Field(title="Can you grant Google Analytics and Search Console access?")


class MarketingAutomationDetails(_Strict):
    crm_platform: _line(1, 100) = Field(title="Which CRM do you use?")
    email_platform: _optional_text(100) = Field(default=None, title="Email marketing platform (if any)")
    monthly_lead_volume: int | None = Field(default=None, ge=0, le=1_000_000,
                                            title="Approximate new leads per month")
    key_workflows: list[ShortItem] = Field(default_factory=list, max_length=10,
                                           title="Workflows to automate first")


class NoDetails(_Strict):
    """Templates without service-specific questions."""


# ------------------------------------------------------------ the form
class _OnboardingBase(_Strict):
    project_name: _line(3, 120) = Field(title="Project name")
    project_description: _text(10, 2000) = Field(title="Short description of the project")
    business_goals: _text(10, 2000) = Field(title="What business goals should this project achieve?")
    scope_requirements: _text(10, 4000) = Field(title="Requirements and scope")
    target_audience: _optional_text(1000) = Field(default=None, title="Target audience")
    desired_delivery_date: Annotated[date | None, AfterValidator(_future_delivery_date)] = Field(
        default=None, title="Desired delivery date")
    communication: CommunicationPreferences = Field(title="Communication preferences")
    additional_notes: _optional_text(2000) = Field(default=None, title="Anything else we should know?")


class WebsiteBuildOnboarding(_OnboardingBase):
    service_details: WebsiteBuildDetails


class SeoRetainerOnboarding(_OnboardingBase):
    service_details: SeoRetainerDetails


class MarketingAutomationOnboarding(_OnboardingBase):
    service_details: MarketingAutomationDetails


class GenericOnboarding(_OnboardingBase):
    service_details: NoDetails = Field(default_factory=NoDetails)


FORMS: dict[str, type[_OnboardingBase]] = {
    "website_build": WebsiteBuildOnboarding,
    "seo_retainer": SeoRetainerOnboarding,
    "marketing_automation_setup": MarketingAutomationOnboarding,
}


def form_for(service_type: str | None) -> type[_OnboardingBase]:
    """The form model for an invitation's service type (decided server-side)."""
    return FORMS.get(service_type or "", GenericOnboarding)


OnboardingForm = _OnboardingBase
