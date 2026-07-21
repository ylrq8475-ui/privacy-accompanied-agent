"""The sole real INTERNET transport used by the Phase 4 weather Demo."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from backend.app.schemas.network import NetworkDestination

from .contracts import (
    ExternalConnectorBoundary,
    ExternalConnectorResponse,
    MAX_RESPONSE_BYTES,
    validate_response_size,
)
from .mock import ListConnectorAuditSink


OPEN_METEO_HOST = "api.open-meteo.com"
OPEN_METEO_PATH = "/v1/forecast"
WEATHER_CITY_COORDINATES: dict[str, tuple[str, str]] = {
    "110000": ("39.9042", "116.4074"),  # 北京
    "310000": ("31.2304", "121.4737"),  # 上海
    "440100": ("23.1291", "113.2644"),  # 广州
    "440300": ("22.5431", "114.0579"),  # 深圳
    "510100": ("30.5728", "104.0668"),  # 成都
}


class ExternalConnectorTransportError(RuntimeError):
    """A real connector call failed or returned an invalid response."""


class WeatherHTTPTransport(Protocol):
    def fetch(self, url: str, timeout_seconds: float) -> bytes:
        """Fetch one allowlisted HTTPS URL and return its bounded body."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ExternalConnectorTransportError("weather provider redirects are disabled")


class UrllibWeatherTransport:
    """Small HTTPS-only transport; no other backend module owns public egress."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(),
            _RejectRedirects(),
        )

    def fetch(self, url: str, timeout_seconds: float) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != OPEN_METEO_HOST
            or parsed.path != OPEN_METEO_PATH
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ExternalConnectorTransportError("weather destination is not allowlisted")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "Spark-Demo/Phase4"},
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                final = urllib.parse.urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname != OPEN_METEO_HOST:
                    raise ExternalConnectorTransportError("weather response changed destination")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except ExternalConnectorTransportError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise ExternalConnectorTransportError("weather provider request failed") from error
        try:
            validate_response_size(body)
        except ValueError as error:
            raise ExternalConnectorTransportError("weather provider response is too large") from error
        return body


_WEATHER_CONDITIONS: tuple[tuple[set[int], str], ...] = (
    ({0}, "clear"),
    ({1, 2}, "partly_cloudy"),
    ({3}, "overcast"),
    ({45, 48}, "fog"),
    ({51, 53, 55, 56, 57}, "drizzle"),
    ({61, 63, 65, 66, 67}, "rain"),
    ({71, 73, 75, 77}, "snow"),
    ({80, 81, 82}, "rain_showers"),
    ({85, 86}, "snow_showers"),
    ({95, 96, 99}, "thunderstorm"),
)


def _condition_for_code(code: int) -> str:
    for codes, condition in _WEATHER_CONDITIONS:
        if code in codes:
            return condition
    return "unknown"


class RealExternalConnector:
    """Validate, privacy-check, audit, then perform one allowlisted weather call."""

    is_real = True

    def __init__(
        self,
        *,
        transport: WeatherHTTPTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.audit_sink = ListConnectorAuditSink()
        self.boundary = ExternalConnectorBoundary(self.audit_sink)
        self.transport = transport or UrllibWeatherTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sent_requests = []

    def send(self, raw_request: Mapping[str, Any]) -> ExternalConnectorResponse:
        approved = self.boundary.prepare(raw_request)
        if approved.destination is not NetworkDestination.WEATHER_API:
            raise ExternalConnectorTransportError("real connector target is not implemented")
        city_code = approved.payload.get("city_code")
        coordinates = WEATHER_CITY_COORDINATES.get(str(city_code))
        if coordinates is None:
            raise ExternalConnectorTransportError("weather city is not allowlisted")
        latitude, longitude = coordinates

        self.sent_requests.append(approved)
        query = urllib.parse.urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,weather_code",
                "timezone": "UTC",
            }
        )
        url = urllib.parse.urlunsplit(("https", OPEN_METEO_HOST, OPEN_METEO_PATH, query, ""))
        started = time.monotonic()
        raw_body = self.transport.fetch(url, approved.timeout_ms / 1000)
        try:
            validate_response_size(raw_body)
        except ValueError as error:
            raise ExternalConnectorTransportError(
                "weather provider response is too large"
            ) from error
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        body = self._normalize(raw_body, city_code)
        return ExternalConnectorResponse(
            request_id=approved.request_id,
            destination=approved.destination,
            network_scope=approved.network_scope,
            status="SUCCEEDED",
            body=body,
            response_size_bytes=len(raw_body),
            latency_ms=latency_ms,
        )

    def _normalize(self, body: bytes, city_code: str) -> dict[str, object]:
        try:
            decoded = json.loads(body.decode("utf-8"))
            current = decoded["current"]
            temperature = current["temperature_2m"]
            weather_code = current["weather_code"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ExternalConnectorTransportError("weather provider response schema rejected") from error
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or isinstance(weather_code, bool)
            or not isinstance(weather_code, int)
        ):
            raise ExternalConnectorTransportError("weather provider value types rejected")
        value = float(temperature)
        if not -80 <= value <= 80:
            raise ExternalConnectorTransportError("weather temperature is out of bounds")
        fetched_at = self.clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ExternalConnectorTransportError("connector clock must be timezone-aware")
        return {
            "city_code": city_code,
            "temperature_c": value,
            "condition": _condition_for_code(weather_code),
            "source": "REAL_API",
            "fetched_at": fetched_at.isoformat(),
            "provider": "OPEN_METEO",
        }
