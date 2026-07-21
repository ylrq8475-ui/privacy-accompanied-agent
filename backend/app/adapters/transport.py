"""Bounded JSON transport that cannot be used for INTERNET egress."""

from __future__ import annotations

import json
import ipaddress
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .contracts import AdapterError


ALLOWED_MODEL_HOSTS = frozenset(
    {
        "stepaudio",
        "step3-vl",
        "companion-stepaudio",
        "companion-step3-vl-2606",
        "track-catalog",
        "external-connector",
        "localhost",
        "127.0.0.1",
        "::1",
    }
)


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    body: dict[str, object]
    latency_ms: int


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        base_url: str,
        path: str,
        body: dict[str, object] | None,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class LocalJSONTransport:
    def __init__(self) -> None:
        # LOCAL model traffic must never inherit an INTERNET proxy from the host.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(
        self,
        method: str,
        base_url: str,
        path: str,
        body: dict[str, object] | None,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        import time

        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not self._allowed_host(parsed.hostname)
        ):
            raise AdapterError("LOCAL_DESTINATION_REJECTED", "model URL is not allowlisted LOCAL infrastructure")
        if not path.startswith("/") or ".." in path:
            raise AdapterError("INVALID_MODEL_PATH", "model path is invalid")
        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                raw = response.read(max_response_bytes + 1)
                status = int(response.status)
        except HTTPError as error:
            raise AdapterError("MODEL_HTTP_ERROR", f"model returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise AdapterError("MODEL_UNAVAILABLE", "LOCAL model service is unavailable") from error
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if len(raw) > max_response_bytes:
            raise AdapterError("MODEL_RESPONSE_TOO_LARGE", "model response exceeded the configured limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterError("MODEL_INVALID_JSON", "model response was not valid UTF-8 JSON") from error
        if not isinstance(decoded, dict):
            raise AdapterError("MODEL_INVALID_ENVELOPE", "model response must be a JSON object")
        return TransportResponse(status=status, body=decoded, latency_ms=latency_ms)

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
