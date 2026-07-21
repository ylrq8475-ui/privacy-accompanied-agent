"""Network scope, destination, and allowlisted payload contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator
from .actions import ACModeValue
from .base import Identifier, StrictModel, require_aware_datetime
from .events import NetworkScope, NetworkScopeValue, PrivacyLevelValue
from .location import CityCode
from .music import PlaylistKeyValue


class NetworkDestination(StrEnum):
    STEP_AUDIO = "STEP_AUDIO"
    STEP3_VL = "STEP3_VL"
    LOCAL_MUSIC = "LOCAL_MUSIC"
    AC_DEVICE = "AC_DEVICE"
    WEATHER_API = "WEATHER_API"
    PUBLIC_MUSIC_API = "PUBLIC_MUSIC_API"


NetworkDestinationValue = Annotated[NetworkDestination, Field(strict=False)]


class WeatherPayload(StrictModel):
    city_code: CityCode


class MusicPayload(StrictModel):
    action: Literal["play"]
    track_id: Literal[
        "calm_piano_01",
        "emotion_relax_01",
        "emotion_comfort_01",
        "emotion_uplift_01",
        "emotion_cooldown_01",
        "emotion_neutral_01",
    ]


class MusicSyncPayload(StrictModel):
    action: Literal["sync_playlist"]
    playlist_ref: PlaylistKeyValue


PublicMusicPayload = MusicPayload | MusicSyncPayload


class ACPayload(StrictModel):
    device_id: Identifier
    mode: ACModeValue
    target_temperature: int = Field(ge=16, le=30)
    duration_minutes: int = Field(ge=1, le=240)


class NetworkCall(StrictModel):
    call_id: Identifier
    source_agent: Identifier
    destination: NetworkDestinationValue
    network_scope: NetworkScopeValue
    payload: dict[str, JsonValue]
    privacy_level: PrivacyLevelValue
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "timestamp")


def validate_connector_route(network_scope: NetworkScope, via_external_connector: bool) -> None:
    """Enforce that only INTERNET traffic traverses the egress connector."""

    if network_scope is NetworkScope.INTERNET and not via_external_connector:
        raise ValueError("INTERNET calls must use external-connector")
    if network_scope in {NetworkScope.LOCAL, NetworkScope.LAN} and via_external_connector:
        raise ValueError("LOCAL and LAN calls must bypass external-connector")
