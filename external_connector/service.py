"""Internal ASGI boundary for the Demo's sole INTERNET egress service."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from backend.app.schemas.music import PlaylistKey

from .audius import AudiusConnectorError, AudiusMusicConnector, AudiusSettings
from .contracts import ConnectorRejectedError, MAX_RESPONSE_BYTES
from .weather import ExternalConnectorTransportError, RealExternalConnector


ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
MAX_AUDIO_BYTES = 8 * 1024 * 1024
_PLAYLIST_PATH = re.compile(r"/v1/audius/playlists/([A-Z]+)/sync")
_PREVIEW_PATH = re.compile(r"/v1/audius/tracks/([A-Za-z0-9]{1,64})/preview")


class ExternalConnectorASGIApp:
    def __init__(
        self,
        *,
        weather: RealExternalConnector | None = None,
        audius: AudiusMusicConnector | None = None,
    ) -> None:
        self.weather = weather or RealExternalConnector()
        self.audius = audius or AudiusMusicConnector(AudiusSettings.from_environment())

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":
            raise RuntimeError("external-connector supports only HTTP and lifespan")

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        try:
            if method == "GET" and path == "/health":
                await _json_response(
                    send,
                    200,
                    {
                        "component": "EXTERNAL_CONNECTOR",
                        "available": True,
                        "status": "READY",
                        "network_scope": "INTERNET",
                        "provider_probe_performed": False,
                        "weather": {
                            "available": True,
                            "status": "CONFIGURED_NOT_CALLED",
                        },
                        "audius": self.audius.health(),
                    },
                )
                return

            if method == "POST" and path == "/v1/weather":
                raw_request = await _read_json(receive)
                result = self.weather.send(raw_request)
                await _json_response(send, 200, result.model_dump(mode="json"))
                return

            playlist_match = _PLAYLIST_PATH.fullmatch(path)
            if method == "POST" and playlist_match:
                playlist_key = PlaylistKey(playlist_match.group(1))
                raw_request = await _read_json(receive)
                result = self.audius.sync_playlist(raw_request, playlist_key)
                await _json_response(
                    send,
                    200,
                    {
                        "playlist_key": result.playlist_key.value,
                        "provider_playlist_id": result.provider_playlist_id,
                        "track_ids": list(result.track_ids),
                        "source_count": result.source_count,
                        "truncated": result.truncated,
                        "latency_ms": result.latency_ms,
                    },
                )
                return

            preview_match = _PREVIEW_PATH.fullmatch(path)
            if method == "POST" and preview_match:
                raw_request = await _read_json(receive)
                result = self.audius.fetch_preview(raw_request, preview_match.group(1))
                await _binary_response(
                    send,
                    result.audio,
                    result.content_type,
                    {
                        "x-provider-track-id": result.provider_track_id,
                        "x-preview-latency-ms": str(result.latency_ms),
                        "x-preview-size-bytes": str(result.size_bytes),
                    },
                )
                return

            await _json_response(send, 404, {"error": "NOT_FOUND", "request_sent": False})
        except ConnectorRejectedError:
            await _json_response(
                send, 403, {"error": "CONNECTOR_REJECTED", "request_sent": False}
            )
        except AudiusConnectorError as error:
            await _json_response(
                send,
                502 if error.request_sent else 409,
                {"error": error.code, "request_sent": error.request_sent},
            )
        except ExternalConnectorTransportError:
            await _json_response(
                send, 502, {"error": "WEATHER_REQUEST_FAILED", "request_sent": True}
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _json_response(
                send, 400, {"error": "INVALID_JSON", "request_sent": False}
            )
        except (ValidationError, ValueError):
            await _json_response(
                send, 422, {"error": "SCHEMA_REJECTED", "request_sent": False}
            )

    @staticmethod
    async def _lifespan(receive: ASGIReceive, send: ASGISend) -> None:
        while True:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message.get("type") == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def _read_json(receive: ASGIReceive) -> dict[str, Any]:
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("request too large")
        if not message.get("more_body", False):
            break
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    created_at = value.get("created_at")
    if isinstance(created_at, str):
        value["created_at"] = datetime.fromisoformat(created_at)
    return value


async def _json_response(send: ASGISend, status: int, body: dict[str, Any]) -> None:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(encoded)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": encoded})


async def _binary_response(
    send: ASGISend,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str],
) -> None:
    if not body or len(body) > MAX_AUDIO_BYTES:
        raise AudiusConnectorError("AUDIUS_AUDIO_TOO_LARGE", request_sent=True)
    headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    headers.extend(
        (name.encode("ascii"), value.encode("ascii"))
        for name, value in extra_headers.items()
    )
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


app = ExternalConnectorASGIApp()
