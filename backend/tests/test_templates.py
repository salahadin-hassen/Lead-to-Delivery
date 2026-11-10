"""Seeded project templates exist and match the Pydantic definition schema."""

import copy

import pytest
from pydantic import ValidationError

from app.schemas.templates import TemplateDefinition

EXPECTED = {
    "website_build": "Website Build",
    "seo_retainer": "SEO Retainer",
    "marketing_automation_setup": "Marketing Automation Setup",
}


def test_three_templates_are_seeded_and_valid(db_conn) -> None:
    rows = db_conn.execute(
        "SELECT key, name, version, active, definition FROM project_templates ORDER BY key"
    ).fetchall()
    assert {r["key"]: r["name"] for r in rows} == EXPECTED
    for row in rows:
        assert row["active"] is True and row["version"] == 1
        definition = TemplateDefinition.model_validate(row["definition"])
        assert len(definition.milestones) >= 3
        assert sum(len(m.tasks) for m in definition.milestones) >= 7


def _definition(db_conn) -> dict:
    row = db_conn.execute(
        "SELECT definition FROM project_templates WHERE key = 'website_build'"
    ).fetchone()
    return copy.deepcopy(row["definition"])


def test_duplicate_task_keys_are_rejected(db_conn) -> None:
    data = _definition(db_conn)
    data["milestones"][1]["tasks"][0]["key"] = data["milestones"][0]["tasks"][0]["key"]
    with pytest.raises(ValidationError, match="task keys must be unique"):
        TemplateDefinition.model_validate(data)


def test_task_due_after_its_milestone_is_rejected(db_conn) -> None:
    data = _definition(db_conn)
    data["milestones"][0]["tasks"][0]["due_offset_days"] = 30
    with pytest.raises(ValidationError, match="due after its milestone"):
        TemplateDefinition.model_validate(data)


def test_unknown_fields_are_rejected(db_conn) -> None:
    data = _definition(db_conn)
    data["milestones"][0]["tasks"][0]["owner"] = "someone"
    with pytest.raises(ValidationError):
        TemplateDefinition.model_validate(data)
