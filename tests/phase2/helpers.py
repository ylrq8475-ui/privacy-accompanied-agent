from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.app.api import DemoASGIApp


async def request(
    app: DemoASGIApp,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
    if isinstance(body, dict):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, bytes):
        encoded = body
    else:
        encoded = b""
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": encoded, "more_body": False})
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )
    start = next(item for item in outgoing if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"") for item in outgoing if item["type"] == "http.response.body"
    )
    return start["status"], dict(start.get("headers", [])), response_body


async def json_request(
    app: DemoASGIApp,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    status, _, response_body = await request(app, method, path, body)
    return status, json.loads(response_body)
