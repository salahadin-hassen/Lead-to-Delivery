"""A small, stateful fake of the ClickUp v2 endpoints the adapter uses,
served through httpx.MockTransport (no network)."""

import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app import db

TOKEN = "pk_test_" + "a1b2c3d4" * 4  # fake; never a real token
LIST_ID = "901234"


class FakeClickUp:
    def __init__(self, *, token: str = TOKEN, list_id: str = LIST_ID, page_size: int = 100) -> None:
        self.token = token
        self.list_id = list_id
        self.page_size = page_size
        self.tasks: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.post_behavior: Callable[[httpx.Request], httpx.Response | None] | None = None
        self.get_behavior: Callable[[httpx.Request], httpx.Response | None] | None = None
        self.connection_checks: list[dict[str, Any]] = []
        self._next = 1

    # helpers for tests
    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]

    def add_task(self, name: str, description: str = "", created_ms: int | None = None) -> dict[str, Any]:
        task_id = f"86ab{self._next:04d}"
        self._next += 1
        task = {"id": task_id, "name": name, "description": description, "text_content": description,
                "url": f"https://app.clickup.com/t/{task_id}",
                "date_created": str(created_ms or int(time.time() * 1000))}
        self.tasks.append(task)
        return task

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # the fake server
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        stats = db._pool.get_stats() if db._pool is not None else {}
        self.connection_checks.append({
            "held": db.connection_held(),
            "in_use": stats.get("pool_size", 0) - stats.get("pool_available", 0),
        })
        if request.headers.get("authorization") != self.token:
            return httpx.Response(401, json={"err": f"Token invalid {request.headers.get('authorization')}",
                                             "ECODE": "OAUTH_025"})
        match = re.fullmatch(r"/api/v2/list/([^/]+)/task", request.url.path)
        if not match:
            return httpx.Response(404, json={"err": "Route not found", "ECODE": "APP_001"})
        if match.group(1) != self.list_id:
            return httpx.Response(404, json={"err": "List not found", "ECODE": "ITEM_013"})
        if request.method == "POST":
            if self.post_behavior is not None:
                result = self.post_behavior(request)
                if result is not None:
                    return result
            body = json.loads(request.content)
            task = self.add_task(body["name"], body.get("description", ""))
            task["due_date"] = body.get("due_date")
            return httpx.Response(200, json={**task, "status": {"status": "to do"}})
        if request.method == "GET":
            if self.get_behavior is not None:
                result = self.get_behavior(request)
                if result is not None:
                    return result
            page = int(request.url.params.get("page", "0"))
            after = int(request.url.params.get("date_created_gt", "0"))
            visible = [t for t in self.tasks if int(t["date_created"]) > after]
            chunk = visible[page * self.page_size:(page + 1) * self.page_size]
            last = (page + 1) * self.page_size >= len(visible)
            return httpx.Response(200, json={"tasks": chunk, "last_page": last})
        return httpx.Response(405)

    def create_then(self, exc_type: type[Exception]) -> None:
        """Simulate: ClickUp creates the task, then the response is lost."""
        def behaviour(request: httpx.Request) -> None:
            body = json.loads(request.content)
            self.add_task(body["name"], body.get("description", ""))
            raise exc_type("response lost", request=request)
        self.post_behavior = behaviour
