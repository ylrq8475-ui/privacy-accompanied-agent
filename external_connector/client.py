"""Bounded LOCAL clients for the isolated external-connector process."""

from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from backend.app.schemas.music import PlaylistKey

from .audius import (
    MAX_AUDIO_BYTES,
    AudiusConnectorError,
    AudiusPlaylistSnapshot,
    AudiusPreview,
)
from .contracts import (
    ConnectorRejectedError,
    ExternalConnectorBoundary,
    ExternalConnectorResponse,
    MAX_RESPONSE_BYTES,
)
from .mock import ListConnectorAuditSink
from .weather import ExternalConnectorTransportError


DEFAULT_CONNECTOR_URL = "http://127.0.0.1:8030"
CONNECTOR_HOSTS = frozenset({"external-connector", "localhost", "127.0.0.1", "::1"})
_TRACK_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
_CONTENT_TYPES = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/mp3",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/x-wav",
    }
)


class InternalConnectorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InternalHTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    latency_ms: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class InternalConnectorHTTP:
    def __init__(self, base_url: str = DEFAULT_CONNECTOR_URL) -> None:
        self.base_url = self._validate_base_url(base_url)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        *,
        max_response_bytes: int,
    ) -> InternalHTTPResponse:
        if not path.startswith("/") or ".." in path:
            raise InternalConnectorUnavailable("internal connector path rejected")
        encoded = None
        if body is not None:
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise InternalConnectorUnavailable("internal connector request too large")
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", path.lstrip("/")),
            data=encoded,
            method=method,
            headers={"Accept": "*/*", "Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=6.0) as response:
                raw = response.read(max_response_bytes + 1)
                status = int(response.status)
                headers = {key.casefold(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as error:
            raw = error.read(max_response_bytes + 1)
            status = int(error.code)
            headers = {key.casefold(): value for key, value in error.headers.items()}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise InternalConnectorUnavailable(
                "internal external-connector is unavailable"
            ) from error
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if len(raw) > max_response_bytes:
            raise InternalConnectorUnavailable("internal connector response too large")
        return InternalHTTPResponse(status, headers, raw, latency_ms)

    @staticmethod
    def _validate_base_url(value: str) -> str:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not InternalConnectorHTTP._allowed_host(parsed.hostname)
        ):
            raise ValueError("external-connector URL is not allowlisted LOCAL infrastructure")
        return urllib.parse.urlunsplit(
            ("http", parsed.netloc, "", "", "")
        ).rstrip("/")

    @staticmethod
    def _allowed_host(hostname: str | None) -> bool:
        if hostname in CONNECTOR_HOSTS:
            return True
        if hostname is None:
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return address.is_private or address.is_loopback or address.is_link_local


def _decode_json(response: InternalHTTPResponse) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/json":
        raise InternalConnectorUnavailable("internal connector returned non-JSON")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InternalConnectorUnavailable("internal connector returned invalid JSON") from error
    if not isinstance(value, dict):
        raise InternalConnectorUnavailable("internal connector JSON envelope rejected")
    return value


class RemoteWeatherConnector:
    is_real = True

    def __init__(
        self,
        base_url: str = DEFAULT_CONNECTOR_URL,
        *,
        transport: InternalConnectorHTTP | None = None,
    ) -> None:
        self.audit_sink = ListConnectorAuditSink()
        self.boundary = ExternalConnectorBoundary(self.audit_sink)
        self.transport = transport or InternalConnectorHTTP(base_url)
        self.sent_requests = []

    def send(self, raw_request: Mapping[str, Any]) -> ExternalConnectorResponse:
        approved = self.boundary.prepare(raw_request)
        self.sent_requests.append(approved)
        try:
            response = self.transport.request(
                "POST",
                "/v1/weather",
                approved.model_dump(mode="json"),
                max_response_bytes=MAX_RESPONSE_BYTES,
            )
            body = _decode_json(response)
            if response.status != 200:
                if body.get("error") == "CONNECTOR_REJECTED":
                    raise ConnectorRejectedError("remote connector rejected request")
                raise ExternalConnectorTransportError("weather egress request failed")
            return ExternalConnectorResponse.model_validate(body)
        except (InternalConnectorUnavailable, ValidationError) as error:
            raise ExternalConnectorTransportError("weather egress service unavailable") from error


class RemoteAudiusSettings:
    def __init__(self, owner: "RemoteAudiusConnector") -> None:
        self._owner = owner

    @property
    def playlist_urls(self) -> Mapping[PlaylistKey, str]:
        return {
            key: "configured-inside-external-connector"
            for key in self._owner.configured_categories()
        }

    @property
    def configured(self) -> bool:
        return bool(self.playlist_urls)

    @property
    def preview_ready(self) -> bool:
        return self._owner.health().get("preview_ready") is True

    @property
    def sync_ready(self) -> bool:
        return self.configured

    def configured_for(self, playlist_key: PlaylistKey) -> bool:
        return playlist_key in self.playlist_urls


class RemoteAudiusConnector:
    def __init__(
        self,
        base_url: str = DEFAULT_CONNECTOR_URL,
        *,
        transport: InternalConnectorHTTP | None = None,
    ) -> None:
        self.audit_sink = ListConnectorAuditSink()
        self.boundary = ExternalConnectorBoundary(self.audit_sink)
        self.transport = transport or InternalConnectorHTTP(base_url)
        self.sent_requests = []
        self.settings = RemoteAudiusSettings(self)
        self._last_health: dict[str, object] | None = None
        self._health_expires_at = 0.0

    def health(self) -> dict[str, object]:
        now = time.monotonic()
        if self._last_health is not None and now < self._health_expires_at:
            return dict(self._last_health)
        try:
            response = self.transport.request(
                "GET", "/health", None, max_response_bytes=65_536
            )
            body = _decode_json(response)
            audius = body.get("audius")
            if response.status != 200 or not isinstance(audius, dict):
                raise InternalConnectorUnavailable("connector health rejected")
            result = dict(audius)
        except InternalConnectorUnavailable:
            result = {
                "component": "AUDIUS_MUSIC",
                "available": False,
                "status": "CONNECTOR_UNAVAILABLE",
                "latency_ms": 0,
                "preview_ready": False,
                "sync_ready": False,
                "verified_seed_count": 0,
                "configured_categories": [],
            }
        self._last_health = result
        self._health_expires_at = now + 2.0
        return dict(result)

    def configured_categories(self) -> set[PlaylistKey]:
        raw = self.health().get("configured_categories")
        if not isinstance(raw, list):
            return set()
        result: set[PlaylistKey] = set()
        for item in raw:
            try:
                result.add(PlaylistKey(str(item)))
            except ValueError:
                return set()
        return result

    def sync_playlist(
        self,
        raw_request: Mapping[str, Any],
        playlist_key: PlaylistKey,
    ) -> AudiusPlaylistSnapshot:
        approved = self.boundary.prepare(raw_request)
        self.sent_requests.append(approved)
        response = self._request_json(
            f"/v1/audius/playlists/{playlist_key.value}/sync",
            approved.model_dump(mode="json"),
        )
        try:
            returned_key = PlaylistKey(str(response["playlist_key"]))
            provider_id = str(response["provider_playlist_id"])
            raw_tracks = response["track_ids"]
            source_count = int(response["source_count"])
            truncated = response["truncated"]
            latency_ms = int(response["latency_ms"])
        except (KeyError, TypeError, ValueError) as error:
            raise AudiusConnectorError(
                "AUDIUS_PLAYLIST_RESPONSE_REJECTED", request_sent=True
            ) from error
        if (
            returned_key is not playlist_key
            or not _TRACK_ID_PATTERN.fullmatch(provider_id)
            or not isinstance(raw_tracks, list)
            or not raw_tracks
            or len(raw_tracks) > 500
            or any(
                not isinstance(item, str) or not _TRACK_ID_PATTERN.fullmatch(item)
                for item in raw_tracks
            )
            or len(set(raw_tracks)) != len(raw_tracks)
            or not isinstance(truncated, bool)
            or source_count < len(raw_tracks)
            or latency_ms < 0
        ):
            raise AudiusConnectorError(
                "AUDIUS_PLAYLIST_RESPONSE_REJECTED", request_sent=True
            )
        return AudiusPlaylistSnapshot(
            playlist_key=returned_key,
            provider_playlist_id=provider_id,
            track_ids=tuple(raw_tracks),
            source_count=source_count,
            truncated=truncated,
            latency_ms=latency_ms,
        )

    def fetch_preview(
        self,
        raw_request: Mapping[str, Any],
        provider_track_id: str,
    ) -> AudiusPreview:
        if not _TRACK_ID_PATTERN.fullmatch(provider_track_id):
            raise AudiusConnectorError("AUDIUS_TRACK_ID_REJECTED", request_sent=False)
        approved = self.boundary.prepare(raw_request)
        self.sent_requests.append(approved)
        try:
            response = self.transport.request(
                "POST",
                f"/v1/audius/tracks/{provider_track_id}/preview",
                approved.model_dump(mode="json"),
                max_response_bytes=MAX_AUDIO_BYTES,
            )
        except InternalConnectorUnavailable as error:
            raise AudiusConnectorError("CONNECTOR_UNAVAILABLE", request_sent=False) from error
        if response.status != 200:
            self._raise_remote_error(response)
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        returned_track = response.headers.get("x-provider-track-id", "")
        try:
            latency_ms = int(response.headers.get("x-preview-latency-ms", "-1"))
            size_bytes = int(response.headers.get("x-preview-size-bytes", "-1"))
        except ValueError as error:
            raise AudiusConnectorError(
                "AUDIUS_PREVIEW_RESPONSE_REJECTED", request_sent=True
            ) from error
        if (
            content_type not in _CONTENT_TYPES
            or returned_track != provider_track_id
            or latency_ms < 0
            or size_bytes != len(response.body)
            or not response.body
        ):
            raise AudiusConnectorError(
                "AUDIUS_PREVIEW_RESPONSE_REJECTED", request_sent=True
            )
        return AudiusPreview(
            provider_track_id=returned_track,
            audio=response.body,
            content_type=content_type,
            size_bytes=size_bytes,
            latency_ms=latency_ms,
        )

    def _request_json(self, path: str, body: dict[str, object]) -> dict[str, Any]:
        try:
            response = self.transport.request(
                "POST", path, body, max_response_bytes=MAX_RESPONSE_BYTES
            )
        except InternalConnectorUnavailable as error:
            raise AudiusConnectorError("CONNECTOR_UNAVAILABLE", request_sent=False) from error
        if response.status != 200:
            self._raise_remote_error(response)
        try:
            return _decode_json(response)
        except InternalConnectorUnavailable as error:
            raise AudiusConnectorError(
                "AUDIUS_CONNECTOR_RESPONSE_REJECTED", request_sent=True
            ) from error

    @staticmethod
    def _raise_remote_error(response: InternalHTTPResponse) -> None:
        try:
            body = _decode_json(response)
        except InternalConnectorUnavailable as error:
            raise AudiusConnectorError(
                "AUDIUS_CONNECTOR_RESPONSE_REJECTED", request_sent=True
            ) from error
        code = body.get("error")
        request_sent = body.get("request_sent")
        if not isinstance(code, str) or not isinstance(request_sent, bool):
            raise AudiusConnectorError(
                "AUDIUS_CONNECTOR_RESPONSE_REJECTED", request_sent=True
            )
        if code == "CONNECTOR_REJECTED":
            raise ConnectorRejectedError("remote connector rejected request")
        raise AudiusConnectorError(code, request_sent=request_sent)
