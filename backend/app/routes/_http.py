"""Small HTTP helpers shared by routes: bounded body reads, error bodies,
and 422 details that never echo user input."""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError


class BodyTooLarge(Exception):
    pass


async def read_body(request: Request, max_bytes: int) -> bytes:
    """Read the body, refusing more than ``max_bytes`` (declared or streamed)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
        raise BodyTooLarge()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


def is_json(request: Request) -> bool:
    return request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


def error(status: int, code: str, detail: str, headers: dict[str, str] | None = None, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code, **extra}, headers=headers)


def validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    # Same shape as FastAPI's 422, minus "input"/"ctx"/"url": inputs may hold
    # personal data and must not be echoed or logged.
    return [
        {"loc": ["body", *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
