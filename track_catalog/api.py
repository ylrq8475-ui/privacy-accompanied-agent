"""Minimal ASGI app for the LOCAL-only Track Catalog."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from .contracts import CatalogLeaseRequest, CatalogResultRequest, CatalogSnapshotRequest
from .seed import DEFAULT_SEED_PATH, public_catalog, seed_store
from .store import DEFAULT_CATALOG_PATH, CatalogError, CatalogStore


ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
MAX_REQUEST_BYTES = 65_536


class CatalogASGIApp:
    def __init__(
        self,
        store: CatalogStore | None = None,
        *,
        seed_path: str | None = None,
    ) -> None:
        self.store = store or CatalogStore(
            os.environ.get("SPARK_CATALOG_PATH", str(DEFAULT_CATALOG_PATH))
        )
        self.seed_path = seed_path or os.environ.get(
            "SPARK_CATALOG_SEED_PATH", str(DEFAULT_SEED_PATH)
        )
        self.seed_summary: dict[str, object] = {"status": "NOT_LOADED"}

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            raise RuntimeError("track catalog only supports HTTP and lifespan")
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        try:
            if method == "GET" and path == "/health":
                health = self.store.health()
                health["seed"] = self.seed_summary
                await _json_response(send, 200, health)
                return
            if method == "GET" and path == "/v1/catalog/public":
                await _json_response(
                    send, 200, public_catalog(self.store, self.seed_path)
                )
                return
            if method == "PUT" and path == "/v1/catalog/snapshot":
                request = CatalogSnapshotRequest.model_validate(await _read_json(receive))
                response = self.store.replace_snapshot(request)
                await _json_response(send, 200, response.model_dump(mode="json"))
                return
            if method == "POST" and path == "/v1/catalog/lease":
                request = CatalogLeaseRequest.model_validate(await _read_json(receive))
                response = self.store.lease(request)
                await _json_response(send, 200, response.model_dump(mode="json"))
                return
            if method == "POST" and path == "/v1/catalog/result":
                request = CatalogResultRequest.model_validate(await _read_json(receive))
                self.store.record_result(request)
                await _json_response(send, 200, {"status": "RECORDED"})
                return
            await _json_response(send, 404, {"error": "NOT_FOUND"})
        except ValidationError:
            await _json_response(send, 422, {"error": "SCHEMA_REJECTED"})
        except CatalogError as error:
            await _json_response(send, 409, {"error": error.code})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            await _json_response(send, 400, {"error": "INVALID_REQUEST"})

    async def _lifespan(self, receive: ASGIReceive, send: ASGISend) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self.store.initialize()
                self.seed_summary = seed_store(self.store, self.seed_path)
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def _read_json(receive: ASGIReceive) -> dict[str, object]:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise ValueError("invalid ASGI request")
        chunk = bytes(message.get("body", b""))
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    decoded = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("request body must be an object")
    return decoded


async def _json_response(send: ASGISend, status: int, body: dict[str, object]) -> None:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(encoded)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": encoded})


app = CatalogASGIApp()
