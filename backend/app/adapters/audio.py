"""Strict adapter for the existing LOCAL StepAudio service."""

from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .contracts import AdapterError, AdapterHealth, TranscriptResult, TTSResult
from .transport import ALLOWED_MODEL_HOSTS, JsonTransport, LocalJSONTransport


SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
MAX_TTS_WAV_BYTES = 2_097_152
TTS_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class WavTransportResponse:
    body: bytes
    content_type: str
    latency_ms: int


class WavTransport(Protocol):
    def request_wav(
        self,
        base_url: str,
        path: str,
        body: dict[str, object],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> WavTransportResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class LocalWavTransport:
    """Bounded LOCAL-only transport for an in-memory StepAudio WAV response."""

    def __init__(self) -> None:
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request_wav(
        self,
        base_url: str,
        path: str,
        body: dict[str, object],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> WavTransportResponse:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not self._allowed_host(parsed.hostname)
        ):
            raise AdapterError("LOCAL_DESTINATION_REJECTED", "StepAudio URL is not LOCAL")
        if not path.startswith("/") or ".." in path:
            raise AdapterError("INVALID_MODEL_PATH", "StepAudio path is invalid")
        request = Request(
            urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        )
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                raw = response.read(max_response_bytes + 1)
                content_type = str(response.headers.get_content_type())
        except HTTPError as error:
            raise AdapterError("TTS_HTTP_ERROR", f"StepAudio returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise AdapterError("TTS_UNAVAILABLE", "LOCAL StepAudio TTS is unavailable") from error
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if len(raw) > max_response_bytes:
            raise AdapterError("TTS_RESPONSE_TOO_LARGE", "TTS WAV exceeded the configured limit")
        return WavTransportResponse(raw, content_type, latency_ms)

    @staticmethod
    def _allowed_host(hostname: str | None) -> bool:
        if hostname in ALLOWED_MODEL_HOSTS:
            return True
        if hostname is None:
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return address.is_private or address.is_loopback or address.is_link_local


class StepAudioAdapter:
    def __init__(
        self,
        base_url: str,
        filename: str | None,
        *,
        transport: JsonTransport | None = None,
        wav_transport: WavTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.filename = filename
        self.transport = transport or LocalJSONTransport()
        self.wav_transport = wav_transport or LocalWavTransport()

    def health(self) -> AdapterHealth:
        started = time.monotonic()
        try:
            response = self.transport.request(
                "GET", self.base_url, "/health", None,
                timeout_seconds=2.0, max_response_bytes=16_384,
            )
        except AdapterError as error:
            latency = max(0, round((time.monotonic() - started) * 1000))
            return AdapterHealth("STEPAUDIO", False, error.code, latency)
        return AdapterHealth("STEPAUDIO", response.status == 200, "READY", response.latency_ms)

    def transcribe(self) -> TranscriptResult:
        if self.filename is None or not SAFE_FILENAME.fullmatch(self.filename):
            raise AdapterError("ASR_FILENAME_UNAVAILABLE", "a safe shared synthetic audio filename is required")
        response = self.transport.request(
            "POST",
            self.base_url,
            "/v1/audio/transcribe",
            {"filename": self.filename},
            timeout_seconds=15.0,
            max_response_bytes=65_536,
        )
        allowed = {"text", "transcript"}
        if not set(response.body).issubset(allowed):
            raise AdapterError("ASR_INVALID_RESPONSE", "ASR response contains undeclared fields")
        values = [response.body[key] for key in allowed if key in response.body]
        if len(values) != 1 or not isinstance(values[0], str):
            raise AdapterError("ASR_INVALID_RESPONSE", "ASR response must contain one transcript string")
        text = values[0].strip()
        if not 1 <= len(text) <= 500:
            raise AdapterError("ASR_INVALID_RESPONSE", "ASR transcript length is invalid")
        return TranscriptResult(text=text, latency_ms=response.latency_ms)

    def synthesize(self, text: str) -> TTSResult:
        if not 1 <= len(text.strip()) <= 200:
            raise AdapterError("TTS_TEXT_INVALID", "TTS text length is invalid")
        response = self.transport.request(
            "POST",
            self.base_url,
            "/v1/audio/synthesize",
            {"text": text, "voice": "female", "max_tokens": 1024},
            timeout_seconds=15.0,
            max_response_bytes=65_536,
        )
        allowed = {"filename", "audio_file"}
        if not set(response.body).issubset(allowed):
            raise AdapterError("TTS_INVALID_RESPONSE", "TTS response contains undeclared fields")
        values = [response.body[key] for key in allowed if key in response.body]
        if len(values) != 1 or not isinstance(values[0], str) or not SAFE_FILENAME.fullmatch(values[0]):
            raise AdapterError("TTS_INVALID_RESPONSE", "TTS response must contain one safe filename")
        return TTSResult("READY", "STEPAUDIO", values[0], response.latency_ms)

    def synthesize_wav(self, text: str) -> tuple[bytes, int]:
        """Return a validated WAV in memory; never writes synthesized audio."""

        cleaned = text.strip()
        if not 1 <= len(cleaned) <= 200:
            raise AdapterError("TTS_TEXT_INVALID", "TTS text length is invalid")
        response = self.wav_transport.request_wav(
            self.base_url,
            "/v1/audio/synthesize",
            {"text": cleaned, "voice": "female", "max_tokens": 1024},
            timeout_seconds=TTS_TIMEOUT_SECONDS,
            max_response_bytes=MAX_TTS_WAV_BYTES,
        )
        if len(response.body) > MAX_TTS_WAV_BYTES:
            raise AdapterError("TTS_RESPONSE_TOO_LARGE", "TTS WAV exceeded the configured limit")
        if response.content_type.lower() not in {"audio/wav", "audio/x-wav"}:
            raise AdapterError("TTS_CONTENT_TYPE_INVALID", "TTS response was not audio/wav")
        if (
            len(response.body) < 12
            or response.body[:4] != b"RIFF"
            or response.body[8:12] != b"WAVE"
        ):
            raise AdapterError("TTS_WAV_INVALID", "TTS response was not a valid WAV envelope")
        return response.body, response.latency_ms
