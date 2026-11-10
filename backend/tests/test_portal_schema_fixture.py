"""The frontend portal tests render the onboarding form from a copy of the
API's JSON Schema. This keeps that copy identical to what the API serves."""

import json
from pathlib import Path

from app.schemas.onboarding import FORMS

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/portal/fixtures/website_build.schema.json"


def test_frontend_fixture_matches_the_api_schema() -> None:
    assert json.loads(FIXTURE.read_text()) == FORMS["website_build"].model_json_schema()
