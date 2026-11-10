"""Helpers for client-portal tests: invitations via the real webhook, valid
forms per service type, auth headers and row counts."""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from tests.crm_helpers import payload, post

_seq = count(1)

PORTAL_TABLES = (
    "onboarding_submissions", "projects", "milestones", "tasks", "integration_jobs", "activity_events",
)

EXPECTED_SHAPE = {  # template key -> (milestones, tasks), from sql/0003
    "website_build": (4, 10),
    "seo_retainer": (3, 7),
    "marketing_automation_setup": (4, 8),
}


def new_invitation(client, *, service_type: str = "website_build", email: str | None = None,
                   contact_id: str | None = None, company: str | None = "Example Company") -> dict[str, Any]:
    """Create a client + invitation through the signed CRM webhook; returns ids and raw token."""
    n = next(_seq)
    data = payload(
        event_id=f"evt_portal_{n}",
        deal={"external_id": f"deal_portal_{n}", "service_type": service_type, "name": f"Deal {n}"},
        contact={"external_id": contact_id or f"contact_portal_{n}", "email": email or f"client{n}@example.com",
                 "company": company},
    )
    response = post(client, data)
    assert response.status_code == 201, response.text
    body = response.json()
    return {"token": body["onboarding_url"].split("#t=", 1)[1], "client_id": body["client_id"],
            "invitation_id": body["invitation_id"], "deal_name": f"Deal {n}"}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def valid_form(service_type: str = "website_build", **overrides: Any) -> dict[str, Any]:
    details = {
        "website_build": {"has_brand_assets": True, "existing_website_url": "https://acme.example",
                          "estimated_page_count": 8, "cms_preference": "wordpress"},
        "seo_retainer": {"website_url": "https://acme.example", "target_locations": ["Berlin"],
                         "priority_keywords": ["bakery berlin"], "has_analytics_access": True},
        "marketing_automation_setup": {"crm_platform": "HubSpot", "monthly_lead_volume": 300,
                                       "key_workflows": ["Lead nurture"]},
    }[service_type]
    form: dict[str, Any] = {
        "project_name": "Acme Growth Project",
        "project_description": "A new project to grow Acme's online presence.",
        "business_goals": "Increase qualified leads by 30% within six months.",
        "scope_requirements": "Everything described in the proposal, phase one only.",
        "target_audience": "Small business owners in Berlin",
        "desired_delivery_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
        "communication": {"preferred_channel": "email", "update_frequency": "weekly"},
        "additional_notes": None,
        "service_details": details,
    }
    form.update(overrides)
    return form


def submit(client, token: str, form: dict[str, Any] | None = None, service_type: str = "website_build"):
    return client.post("/api/portal/onboarding", json=form or valid_form(service_type), headers=auth(token))


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in PORTAL_TABLES}


def walk_keys(value: Any) -> set[str]:
    """Every dict key anywhere in a JSON document."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            keys |= walk_keys(v)
    elif isinstance(value, list):
        for v in value:
            keys |= walk_keys(v)
    return keys


FORBIDDEN_KEYS = {
    "token_hash", "token", "clickup_task_id", "clickup_url", "idempotency_key", "coalesce_key",
    "job_type", "crm_source", "crm_external_id", "crm_deal_id", "client_id", "invitation_id",
    "assignee_name", "assignee_email", "client_visible", "template_task_key", "sync_version",
    "payload", "data", "email",
}
