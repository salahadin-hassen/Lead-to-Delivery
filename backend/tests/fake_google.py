"""A stateful fake of Google's OAuth token endpoint and the Sheets API v4
subset the adapter uses, served through httpx.MockTransport (no network)."""

import base64
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from app import db

ACCESS_TOKEN = "ya29.SYNTHETIC-access-token-" + "q" * 40
SPREADSHEET_ID = "1SyntheticSpreadsheetId_abcdefghijklmnop"
SERVICE_EMAIL = "ltd-sync@synthetic-project.iam.gserviceaccount.com"
HEADER = ["project_id", "client_name", "company_name", "project_name", "template_name", "project_status",
          "progress_percent", "completed_tasks", "total_tasks", "start_date", "due_date", "clickup_url", "updated_at"]

_A1 = re.compile(r"^'?(?P<sheet>.+?)'?!(?P<c1>[A-Z]+)(?P<r1>\d*):(?P<c2>[A-Z]+)(?P<r2>\d*)$")


def _col(letters: str) -> int:
    return ord(letters) - ord("A")


def write_key_file(directory: Path, private_key_pem: str, **overrides: Any) -> Path:
    info = {"type": "service_account", "project_id": "synthetic-project", "private_key_id": "abc123",
            "private_key": private_key_pem, "client_email": SERVICE_EMAIL, "client_id": "1234567890",
            "token_uri": "https://oauth2.googleapis.com/token", **overrides}
    path = directory / "sa.json"
    path.write_text(json.dumps({k: v for k, v in info.items() if v is not None}))
    return path


class FakeGoogle:
    def __init__(self, *, sheet: str = "Projects", header: list[str] | None = None) -> None:
        self.sheet = sheet
        self.rows: list[list[Any]] = [list(header if header is not None else HEADER)]
        self.requests: list[httpx.Request] = []
        self.token_requests: list[dict[str, Any]] = []
        self.behavior: Callable[[str, httpx.Request], httpx.Response | None] | None = None
        self.token_behavior: Callable[[httpx.Request], httpx.Response | None] | None = None
        self.checks: list[dict[str, Any]] = []

    # --- helpers for tests
    def ids(self) -> list[str]:
        return [str(r[0]) for r in self.rows[1:] if r and r[0] not in ("", None)]

    def find(self, project_id: str) -> list[list[Any]]:
        return [r for r in self.rows[1:] if r and str(r[0]) == project_id]

    def calls(self, kind: str) -> list[httpx.Request]:
        return [r for r in self.requests if self.kind(r) == kind]

    @staticmethod
    def kind(request: httpx.Request) -> str:
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            return "token"
        if path.endswith("values:batchGet"):
            return "read"
        if path.endswith(":append"):
            return "append"
        if request.method == "PUT":
            return "update"
        if path.endswith(":batchUpdate"):
            return "batch_update"
        if request.method == "GET" and re.match(r"^/v4/spreadsheets/[^/]+$", path):
            return "meta"
        return "other"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- server
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        stats = db._pool.get_stats() if db._pool is not None else {}
        self.checks.append({"held": db.connection_held(),
                            "in_use": stats.get("pool_size", 0) - stats.get("pool_available", 0)})
        kind = self.kind(request)
        if kind == "token":
            return self._token(request)
        if request.headers.get("authorization") != f"Bearer {ACCESS_TOKEN}":
            return self._error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        if self.behavior is not None:
            result = self.behavior(kind, request)
            if result is not None:
                return result
        match = re.match(r"^/v4/spreadsheets/([^/:]+)(.*)$", request.url.path)
        if not match or match.group(1) != SPREADSHEET_ID:
            return self._error(404, "NOT_FOUND", "Requested entity was not found.")
        if kind == "read":
            return self._batch_get(request)
        if kind == "append":
            return self._append(request)
        if kind == "update":
            return self._update(request)
        if kind == "meta":
            return httpx.Response(200, json={"sheets": [{"properties": {"sheetId": 777, "title": self.sheet}}]})
        if kind == "batch_update":
            return self._batch_update(request)
        return httpx.Response(404)

    def _batch_update(self, request: httpx.Request) -> httpx.Response:
        for req in json.loads(request.content)["requests"]:
            rng = req["deleteDimension"]["range"]
            assert rng["sheetId"] == 777 and rng["dimension"] == "ROWS"
            del self.rows[rng["startIndex"]:rng["endIndex"]]
        return httpx.Response(200, json={"spreadsheetId": SPREADSHEET_ID, "replies": [{}]})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        payload = json.loads(base64.urlsafe_b64decode(form["assertion"].split(".")[1] + "=="))
        self.token_requests.append({"form": form, "claims": payload})
        if self.token_behavior is not None:
            result = self.token_behavior(request)
            if result is not None:
                return result
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "Bearer"})

    @staticmethod
    def _error(status: int, state: str, message: str, reason: str | None = None) -> httpx.Response:
        error: dict[str, Any] = {"code": status, "message": message, "status": state}
        if reason:
            error["errors"] = [{"reason": reason, "message": message}]
        return httpx.Response(status, json={"error": error})

    def _range(self, text: str):
        m = _A1.match(text)
        if not m or m["sheet"].replace("''", "'") != self.sheet:
            return None
        return m

    def _batch_get(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("valueRenderOption") == "UNFORMATTED_VALUE"
        out = []
        for text in request.url.params.get_list("ranges"):
            m = self._range(text)
            if m is None:
                return self._error(400, "INVALID_ARGUMENT", f"Unable to parse range: {text}", "badRequest")
            r1 = int(m["r1"] or 1)
            r2 = int(m["r2"] or len(self.rows))
            c1, c2 = _col(m["c1"]), _col(m["c2"])
            values = []
            for row in self.rows[r1 - 1:r2]:
                cells = list(row[c1:c2 + 1])
                while cells and cells[-1] in ("", None):
                    cells.pop()
                values.append(cells)
            while values and not values[-1]:
                values.pop()
            out.append({"range": text, "majorDimension": "ROWS", **({"values": values} if values else {})})
        return httpx.Response(200, json={"spreadsheetId": SPREADSHEET_ID, "valueRanges": out})

    def _target(self, request: httpx.Request) -> str:
        rest = request.url.path.split("/values/", 1)[1]
        return rest[: -len(":append")] if rest.endswith(":append") else rest

    def _append(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("valueInputOption") == "RAW"
        assert request.url.params.get("insertDataOption") == "INSERT_ROWS"
        if self._range(self._target(request)) is None:
            return self._error(400, "INVALID_ARGUMENT", "Unable to parse range", "badRequest")
        values = json.loads(request.content)["values"][0]
        while len(self.rows) > 1 and not any(c not in ("", None) for c in self.rows[-1]):
            self.rows.pop()
        self.rows.append(list(values))
        n = len(self.rows)
        return httpx.Response(200, json={"spreadsheetId": SPREADSHEET_ID, "tableRange": f"'{self.sheet}'!A1:M{n - 1}",
                                         "updates": {"updatedRange": f"'{self.sheet}'!A{n}:M{n}", "updatedRows": 1,
                                                     "updatedColumns": len(values), "updatedCells": len(values)}})

    def _update(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("valueInputOption") == "RAW"
        m = self._range(self._target(request))
        if m is None:
            return self._error(400, "INVALID_ARGUMENT", "Unable to parse range", "badRequest")
        row = int(m["r1"])
        values = json.loads(request.content)["values"][0]
        while len(self.rows) < row:
            self.rows.append([])
        self.rows[row - 1] = list(values)
        return httpx.Response(200, json={"spreadsheetId": SPREADSHEET_ID, "updatedRange": f"'{self.sheet}'!A{row}:M{row}",
                                         "updatedRows": 1, "updatedColumns": len(values), "updatedCells": len(values)})
