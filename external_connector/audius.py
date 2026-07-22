"""Audius preview adapter owned by the sole INTERNET egress service.

Credentials and provider URLs are deliberately kept out of return values,
exceptions, audit records, and application logs.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from backend.app.schemas.network import NetworkDestination
from backend.app.schemas.music import PlaylistKey

from .contracts import ExternalConnectorBoundary, MAX_RESPONSE_BYTES
from .mock import ListConnectorAuditSink


AUDIUS_API_HOST = "api.audius.co"
AUDIUS_TIMEOUT_SECONDS = 5.0
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_PLAYLIST_TRACKS = 500
DEFAULT_PLAYLIST_CONFIG = Path("data/audius_playlists.local.json")
DEFAULT_SEED_CONFIG = Path(
    "data/music/seeds/audius_mood_playlist_20_tracks.resolved.json"
)
_TRACK_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
_CREDENTIAL_PATTERN = re.compile(r"^[\x21-\x7e]{1,1024}$")
_DNS_NAME_PATTERN = re.compile(
    r"^(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_AUDIO_CONTENT_TYPES = frozenset(
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


def _read_secret(values: Mapping[str, str], name: str) -> str:
    direct = values.get(name, "").strip()
    file_value = values.get(f"{name}_FILE", "").strip()
    if direct and file_value:
        return ""
    if direct:
        return direct
    if not file_value:
        return ""
    try:
        return Path(file_value).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _validate_playlist_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("playlist URL is invalid") from error
    segments = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "audius.co"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or len(segments) != 2
        or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item) for item in segments)
    ):
        raise ValueError("playlist URL is not an allowlisted Audius canonical URL")
    return f"https://audius.co/{segments[0]}/{segments[1]}"


def _load_playlist_urls(path: Path) -> dict[PlaylistKey, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping) or set(raw) != {"version", "playlists"}:
        return {}
    playlists = raw.get("playlists")
    if raw.get("version") != 1 or not isinstance(playlists, Mapping):
        return {}
    result: dict[PlaylistKey, str] = {}
    try:
        for raw_key, raw_url in playlists.items():
            playlist_key = PlaylistKey(str(raw_key))
            if not isinstance(raw_url, str):
                return {}
            result[playlist_key] = _validate_playlist_url(raw_url.strip())
    except (ValueError, TypeError):
        return {}
    if len(set(result.values())) != len(result):
        return {}
    return result


def _load_verified_seed_ids(path: Path) -> frozenset[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        return frozenset()
    tracks = raw.get("tracks")
    if not isinstance(tracks, list):
        return frozenset()
    result: set[str] = set()
    for item in tracks:
        if not isinstance(item, Mapping):
            return frozenset()
        track_id = item.get("track_id")
        metadata = item.get("provider_metadata")
        if (
            not isinstance(track_id, str)
            or not _TRACK_ID_PATTERN.fullmatch(track_id)
            or item.get("track_id_resolution") != "resolved_from_permalink"
            or not isinstance(metadata, Mapping)
            or metadata.get("is_streamable") is not True
            or metadata.get("is_unlisted") is not False
            or metadata.get("is_stream_gated") is not False
            or track_id in result
        ):
            return frozenset()
        result.add(track_id)
    return frozenset(result)


class AudiusConnectorError(RuntimeError):
    """A safe, credential-free failure suitable for fallback reason codes."""

    def __init__(self, code: str, *, request_sent: bool) -> None:
        super().__init__(code)
        self.code = code
        self.request_sent = request_sent


@dataclass(frozen=True, slots=True)
class AudiusSettings:
    enabled: bool
    api_key: str = field(repr=False)
    bearer_token: str = field(repr=False)
    playlist_urls: Mapping[PlaylistKey, str] = field(repr=False)
    configured: bool
    verified_seed_ids: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        config_path: str | Path | None = None,
        seed_path: str | Path | None = None,
    ) -> "AudiusSettings":
        values = os.environ if environ is None else environ
        enabled = values.get("SPARK_AUDIUS_ENABLED", "").strip().casefold() == "true"
        api_key = _read_secret(values, "SPARK_AUDIUS_API_KEY")
        bearer_token = _read_secret(values, "SPARK_AUDIUS_BEARER_TOKEN")
        selected_config = config_path or values.get(
            "SPARK_AUDIUS_PLAYLIST_CONFIG", str(DEFAULT_PLAYLIST_CONFIG)
        )
        playlist_urls = _load_playlist_urls(Path(selected_config))
        selected_seed = seed_path or values.get(
            "SPARK_AUDIUS_SEED_CONFIG", str(DEFAULT_SEED_CONFIG)
        )
        verified_seed_ids = _load_verified_seed_ids(Path(selected_seed))
        credentials_valid = all(
            not value or _CREDENTIAL_PATTERN.fullmatch(value)
            for value in (api_key, bearer_token)
        )
        configured = bool(
            enabled
            and credentials_valid
            and playlist_urls
        )
        return cls(
            enabled,
            api_key,
            bearer_token,
            playlist_urls,
            configured,
            verified_seed_ids,
        )

    @property
    def preview_ready(self) -> bool:
        credentials_valid = all(
            not value or _CREDENTIAL_PATTERN.fullmatch(value)
            for value in (self.api_key, self.bearer_token)
        )
        return bool(
            self.enabled
            and credentials_valid
            and (self.verified_seed_ids or self.configured)
        )

    @property
    def sync_ready(self) -> bool:
        return self.configured

    def configured_for(self, playlist_key: PlaylistKey) -> bool:
        return self.configured and playlist_key in self.playlist_urls


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ValidatedContentTarget:
    hostname: str
    addresses: tuple[str, ...]
    request_target: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AudiusPreview:
    provider_track_id: str
    audio: bytes = field(repr=False)
    content_type: str
    size_bytes: int
    latency_ms: int
    preview: bool = True


@dataclass(frozen=True, slots=True)
class AudiusPlaylistSnapshot:
    playlist_key: PlaylistKey
    provider_playlist_id: str
    track_ids: tuple[str, ...]
    source_count: int
    truncated: bool
    latency_ms: int


class AudiusHTTPTransport(Protocol):
    def fetch_api(
        self,
        request_target: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HTTPResponse:
        """Fetch one fixed-host Audius API resource."""

    def fetch_content(
        self,
        target: ValidatedContentTarget,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HTTPResponse:
        """Fetch one already validated and DNS-pinned content node resource."""


Resolver = Callable[[str, int], list[tuple[Any, ...]]]
Decoder = Callable[[bytes], object]


def _default_resolver(hostname: str, port: int) -> list[tuple[Any, ...]]:
    return socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


def validate_content_url(
    url: str,
    *,
    resolver: Resolver = _default_resolver,
) -> ValidatedContentTarget:
    """Reject URL tricks and pin a content-node request to validated public IPs."""

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise AudiusConnectorError("AUDIUS_CONTENT_URL_REJECTED", request_sent=True) from error
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise AudiusConnectorError("AUDIUS_CONTENT_URL_REJECTED", request_sent=True)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise AudiusConnectorError("AUDIUS_CONTENT_URL_REJECTED", request_sent=True)
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise AudiusConnectorError("AUDIUS_CONTENT_URL_REJECTED", request_sent=True) from error
    if not _DNS_NAME_PATTERN.fullmatch(ascii_hostname):
        raise AudiusConnectorError("AUDIUS_CONTENT_URL_REJECTED", request_sent=True)

    try:
        answers = resolver(ascii_hostname, 443)
    except OSError as error:
        raise AudiusConnectorError("AUDIUS_CONTENT_DNS_FAILED", request_sent=True) from error
    addresses: list[str] = []
    for answer in answers:
        try:
            raw_address = str(answer[4][0])
            address = ipaddress.ip_address(raw_address)
        except (IndexError, TypeError, ValueError) as error:
            raise AudiusConnectorError("AUDIUS_CONTENT_DNS_REJECTED", request_sent=True) from error
        if not address.is_global:
            raise AudiusConnectorError("AUDIUS_CONTENT_DNS_REJECTED", request_sent=True)
        canonical = address.compressed
        if canonical not in addresses:
            addresses.append(canonical)
    if not addresses:
        raise AudiusConnectorError("AUDIUS_CONTENT_DNS_FAILED", request_sent=True)
    path = parsed.path or "/"
    request_target = urllib.parse.urlunsplit(("", "", path, parsed.query, ""))
    return ValidatedContentTarget(ascii_hostname, tuple(addresses), request_target)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        address: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, 443, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class PinnedHTTPSAudiusTransport:
    """No-proxy, no-redirect HTTPS transport with TLS hostname verification."""

    def __init__(
        self,
        *,
        resolver: Resolver = _default_resolver,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self._resolver = resolver
        self._context = context or ssl.create_default_context()

    def fetch_api(
        self,
        request_target: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HTTPResponse:
        if not (
            request_target.startswith("/v1/tracks/")
            or request_target.startswith("/v1/resolve?")
        ):
            raise AudiusConnectorError("AUDIUS_API_TARGET_REJECTED", request_sent=False)
        addresses = self._public_addresses(AUDIUS_API_HOST, request_sent=False)
        return self._fetch(
            AUDIUS_API_HOST,
            addresses[0],
            request_target,
            headers,
            timeout_seconds,
            max_bytes,
        )

    def fetch_content(
        self,
        target: ValidatedContentTarget,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HTTPResponse:
        return self._fetch(
            target.hostname,
            target.addresses[0],
            target.request_target,
            headers,
            timeout_seconds,
            max_bytes,
        )

    def _public_addresses(self, hostname: str, *, request_sent: bool) -> tuple[str, ...]:
        try:
            answers = self._resolver(hostname, 443)
            addresses = tuple(
                dict.fromkeys(ipaddress.ip_address(item[4][0]).compressed for item in answers)
            )
        except (OSError, IndexError, TypeError, ValueError) as error:
            raise AudiusConnectorError("AUDIUS_API_DNS_FAILED", request_sent=request_sent) from error
        if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
            raise AudiusConnectorError("AUDIUS_API_DNS_REJECTED", request_sent=request_sent)
        return addresses

    def _fetch(
        self,
        hostname: str,
        address: str,
        request_target: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HTTPResponse:
        connection = _PinnedHTTPSConnection(
            hostname,
            address,
            timeout=timeout_seconds,
            context=self._context,
        )
        try:
            connection.request("GET", request_target, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(max_bytes + 1)
            response_headers = {key.casefold(): value for key, value in response.getheaders()}
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise AudiusConnectorError("AUDIUS_TRANSPORT_FAILED", request_sent=True) from error
        finally:
            connection.close()
        if len(body) > max_bytes:
            raise AudiusConnectorError("AUDIUS_RESPONSE_TOO_LARGE", request_sent=True)
        return HTTPResponse(response.status, response_headers, body)


def _default_decoder(audio: bytes) -> object:
    try:
        import miniaudio

        return miniaudio.decode(audio)
    except Exception as error:
        raise AudiusConnectorError("AUDIUS_AUDIO_DECODE_FAILED", request_sent=True) from error


class AudiusMusicConnector:
    """Sync approved playlists and fetch previews through the sole egress boundary."""

    def __init__(
        self,
        settings: AudiusSettings,
        *,
        transport: AudiusHTTPTransport | None = None,
        resolver: Resolver = _default_resolver,
        decoder: Decoder = _default_decoder,
    ) -> None:
        self.settings = settings
        self.audit_sink = ListConnectorAuditSink()
        self.boundary = ExternalConnectorBoundary(self.audit_sink)
        self.transport = transport or PinnedHTTPSAudiusTransport(resolver=resolver)
        self.resolver = resolver
        self.decoder = decoder
        self.sent_requests = []
        self._approved_track_ids = set(settings.verified_seed_ids)
        if settings.preview_ready and settings.sync_ready:
            self._status = "PREVIEW_AND_SYNC_READY_NOT_PROBED"
        elif settings.preview_ready:
            self._status = "SEED_PREVIEW_READY_NOT_PROBED"
        else:
            self._status = "NOT_CONFIGURED"
        self._latency_ms = 0
        self._lock = RLock()

    def health(self) -> dict[str, object]:
        with self._lock:
            return {
                "component": "AUDIUS_MUSIC",
                "available": self.settings.preview_ready or self.settings.sync_ready,
                "status": self._status,
                "latency_ms": self._latency_ms,
                "preview_ready": self.settings.preview_ready,
                "sync_ready": self.settings.sync_ready,
                "verified_seed_count": len(self.settings.verified_seed_ids),
                "configured_categories": sorted(
                    item.value for item in self.settings.playlist_urls
                ),
            }

    def sync_playlist(
        self,
        raw_request: Mapping[str, Any],
        playlist_key: PlaylistKey,
    ) -> AudiusPlaylistSnapshot:
        if not self.settings.configured_for(playlist_key):
            raise AudiusConnectorError("PLAYLIST_NOT_CONFIGURED", request_sent=False)
        approved = self.boundary.prepare(raw_request)
        expected = {
            "action": "sync_playlist",
            "playlist_ref": playlist_key.value,
        }
        if (
            approved.destination is not NetworkDestination.PUBLIC_MUSIC_API
            or approved.payload != expected
        ):
            raise AudiusConnectorError("AUDIUS_PAYLOAD_REJECTED", request_sent=False)
        self.sent_requests.append(approved)
        started = time.monotonic()
        try:
            configured_url = self.settings.playlist_urls[playlist_key]
            query = self._api_query({"url": configured_url})
            response = self.transport.fetch_api(
                f"/v1/resolve?{query}",
                self._api_headers(),
                AUDIUS_TIMEOUT_SECONDS,
                MAX_RESPONSE_BYTES,
            )
            decoded = self._decode_api_json(response, "AUDIUS_PLAYLIST")
            provider_id, track_ids, source_count, truncated = self._validate_playlist(
                decoded
            )
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            snapshot = AudiusPlaylistSnapshot(
                playlist_key=playlist_key,
                provider_playlist_id=provider_id,
                track_ids=track_ids,
                source_count=source_count,
                truncated=truncated,
                latency_ms=latency_ms,
            )
        except AudiusConnectorError:
            self._mark_degraded(started)
            raise
        with self._lock:
            self._approved_track_ids.update(snapshot.track_ids)
            self._status = "READY"
            self._latency_ms = snapshot.latency_ms
        return snapshot

    def fetch_preview(
        self,
        raw_request: Mapping[str, Any],
        provider_track_id: str,
    ) -> AudiusPreview:
        if not self.settings.preview_ready:
            raise AudiusConnectorError("NOT_CONFIGURED", request_sent=False)
        if not _TRACK_ID_PATTERN.fullmatch(provider_track_id):
            raise AudiusConnectorError("AUDIUS_TRACK_ID_REJECTED", request_sent=False)
        with self._lock:
            approved_track = provider_track_id in self._approved_track_ids
        if not approved_track:
            raise AudiusConnectorError("AUDIUS_TRACK_NOT_APPROVED", request_sent=False)
        approved = self.boundary.prepare(raw_request)
        if approved.destination is not NetworkDestination.PUBLIC_MUSIC_API:
            raise AudiusConnectorError("AUDIUS_DESTINATION_REJECTED", request_sent=False)
        if approved.payload.get("action") != "play":
            raise AudiusConnectorError("AUDIUS_PAYLOAD_REJECTED", request_sent=False)

        self.sent_requests.append(approved)
        started = time.monotonic()
        try:
            metadata = self._fetch_metadata(provider_track_id)
            self._validate_metadata(metadata, provider_track_id)
            content_url = self._fetch_stream_url(provider_track_id)
            target = validate_content_url(content_url, resolver=self.resolver)
            response = self.transport.fetch_content(
                target,
                {"Accept": "audio/*", "User-Agent": "Spark-Demo/AudiusPreview"},
                AUDIUS_TIMEOUT_SECONDS,
                MAX_AUDIO_BYTES,
            )
            self._validate_audio_response(response)
            self.decoder(response.body)
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            preview = AudiusPreview(
                provider_track_id=provider_track_id,
                audio=response.body,
                content_type=_content_type(response.headers),
                size_bytes=len(response.body),
                latency_ms=latency_ms,
            )
        except AudiusConnectorError:
            self._mark_degraded(started)
            raise
        with self._lock:
            self._status = "READY"
            self._latency_ms = preview.latency_ms
        return preview

    def _api_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Spark-Demo/AudiusPreview",
        }
        if self.settings.bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.bearer_token}"
        return headers

    def _api_query(self, values: Mapping[str, str]) -> str:
        query = dict(values)
        if self.settings.api_key:
            query["api_key"] = self.settings.api_key
        return urllib.parse.urlencode(query)

    def _fetch_metadata(self, provider_track_id: str) -> Mapping[str, Any]:
        track = urllib.parse.quote(provider_track_id, safe="")
        query = self._api_query({})
        suffix = f"?{query}" if query else ""
        response = self.transport.fetch_api(
            f"/v1/tracks/{track}{suffix}",
            self._api_headers(),
            AUDIUS_TIMEOUT_SECONDS,
            MAX_RESPONSE_BYTES,
        )
        return self._decode_api_json(response, "AUDIUS_METADATA")

    def _fetch_stream_url(self, provider_track_id: str) -> str:
        track = urllib.parse.quote(provider_track_id, safe="")
        query = self._api_query({"preview": "true", "no_redirect": "true"})
        response = self.transport.fetch_api(
            f"/v1/tracks/{track}/stream?{query}",
            self._api_headers(),
            AUDIUS_TIMEOUT_SECONDS,
            MAX_RESPONSE_BYTES,
        )
        decoded = self._decode_api_json(response, "AUDIUS_STREAM")
        data = decoded.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, Mapping) and isinstance(data.get("url"), str):
            return str(data["url"])
        raise AudiusConnectorError("AUDIUS_STREAM_SCHEMA_REJECTED", request_sent=True)

    @staticmethod
    def _decode_api_json(response: HTTPResponse, prefix: str) -> Mapping[str, Any]:
        if response.status != 200:
            raise AudiusConnectorError(
                f"{prefix}_HTTP_{response.status}", request_sent=True
            )
        if _content_type(response.headers) != "application/json":
            raise AudiusConnectorError(f"{prefix}_CONTENT_TYPE_REJECTED", request_sent=True)
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise AudiusConnectorError(f"{prefix}_TOO_LARGE", request_sent=True)
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AudiusConnectorError(f"{prefix}_JSON_REJECTED", request_sent=True) from error
        if not isinstance(decoded, Mapping):
            raise AudiusConnectorError(f"{prefix}_SCHEMA_REJECTED", request_sent=True)
        return decoded

    def _validate_metadata(
        self, decoded: Mapping[str, Any], provider_track_id: str
    ) -> None:
        data = decoded.get("data")
        if not isinstance(data, Mapping):
            raise AudiusConnectorError("AUDIUS_METADATA_SCHEMA_REJECTED", request_sent=True)
        if data.get("id") != provider_track_id:
            raise AudiusConnectorError("AUDIUS_TRACK_ID_MISMATCH", request_sent=True)
        if data.get("is_streamable") is not True:
            raise AudiusConnectorError("AUDIUS_TRACK_NOT_STREAMABLE", request_sent=True)
        if data.get("is_unlisted") is not False:
            raise AudiusConnectorError("AUDIUS_TRACK_NOT_PUBLIC", request_sent=True)
        if data.get("is_stream_gated") is not False:
            raise AudiusConnectorError("AUDIUS_TRACK_GATED", request_sent=True)
        if data.get("stream_conditions") not in (None, {}):
            raise AudiusConnectorError("AUDIUS_TRACK_GATED", request_sent=True)
        if data.get("allowed_api_keys") not in (None, []):
            raise AudiusConnectorError("AUDIUS_TRACK_GATED", request_sent=True)
        if data.get("is_delete") is True or data.get("is_deleted") is True:
            raise AudiusConnectorError("AUDIUS_TRACK_UNAVAILABLE", request_sent=True)
        if data.get("is_available") is False:
            raise AudiusConnectorError("AUDIUS_TRACK_UNAVAILABLE", request_sent=True)

    @staticmethod
    def _validate_playlist(
        decoded: Mapping[str, Any],
    ) -> tuple[str, tuple[str, ...], int, bool]:
        data = decoded.get("data")
        if not isinstance(data, Mapping):
            raise AudiusConnectorError(
                "AUDIUS_PLAYLIST_SCHEMA_REJECTED", request_sent=True
            )
        provider_id = data.get("id")
        contents = data.get("playlist_contents")
        if contents is None:
            contents = data.get("playlistContents")
        if (
            not isinstance(provider_id, str)
            or not _TRACK_ID_PATTERN.fullmatch(provider_id)
            or not isinstance(contents, list)
        ):
            raise AudiusConnectorError(
                "AUDIUS_PLAYLIST_SCHEMA_REJECTED", request_sent=True
            )
        if data.get("is_private") is not False:
            raise AudiusConnectorError("AUDIUS_PLAYLIST_NOT_PUBLIC", request_sent=True)
        if data.get("is_album") is True:
            raise AudiusConnectorError("AUDIUS_PLAYLIST_RESOURCE_REJECTED", request_sent=True)
        if data.get("is_stream_gated") not in (None, False):
            raise AudiusConnectorError("AUDIUS_PLAYLIST_GATED", request_sent=True)
        if data.get("stream_conditions") not in (None, {}):
            raise AudiusConnectorError("AUDIUS_PLAYLIST_GATED", request_sent=True)
        source_count = len(contents)
        unique: list[str] = []
        for entry in contents:
            if not isinstance(entry, Mapping):
                raise AudiusConnectorError(
                    "AUDIUS_PLAYLIST_SCHEMA_REJECTED", request_sent=True
                )
            track_id = entry.get("track_id")
            if track_id is None:
                track_id = entry.get("trackId")
            if not isinstance(track_id, str) or not _TRACK_ID_PATTERN.fullmatch(track_id):
                raise AudiusConnectorError(
                    "AUDIUS_PLAYLIST_TRACK_REJECTED", request_sent=True
                )
            if track_id not in unique and len(unique) < MAX_PLAYLIST_TRACKS:
                unique.append(track_id)
        if not unique:
            raise AudiusConnectorError("AUDIUS_PLAYLIST_EMPTY", request_sent=True)
        return provider_id, tuple(unique), source_count, source_count > len(unique)

    def _mark_degraded(self, started: float) -> None:
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        with self._lock:
            self._status = "DEGRADED"
            self._latency_ms = latency_ms

    @staticmethod
    def _validate_audio_response(response: HTTPResponse) -> None:
        if response.status != 200:
            raise AudiusConnectorError(
                f"AUDIUS_CONTENT_HTTP_{response.status}", request_sent=True
            )
        content_type = _content_type(response.headers)
        if content_type not in _AUDIO_CONTENT_TYPES:
            raise AudiusConnectorError("AUDIUS_AUDIO_CONTENT_TYPE_REJECTED", request_sent=True)
        content_encoding = response.headers.get("content-encoding", "identity").casefold()
        if content_encoding not in ("", "identity"):
            raise AudiusConnectorError("AUDIUS_AUDIO_ENCODING_REJECTED", request_sent=True)
        raw_length = response.headers.get("content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError as error:
                raise AudiusConnectorError("AUDIUS_AUDIO_LENGTH_REJECTED", request_sent=True) from error
            if declared_length < 0 or declared_length > MAX_AUDIO_BYTES:
                raise AudiusConnectorError("AUDIUS_AUDIO_TOO_LARGE", request_sent=True)
        if not response.body:
            raise AudiusConnectorError("AUDIUS_AUDIO_EMPTY", request_sent=True)
        if len(response.body) > MAX_AUDIO_BYTES:
            raise AudiusConnectorError("AUDIUS_AUDIO_TOO_LARGE", request_sent=True)


def _content_type(headers: Mapping[str, str]) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().casefold()
