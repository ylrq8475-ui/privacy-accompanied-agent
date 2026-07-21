"""Strict Phase 4 contracts for normalized weather data."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .base import StrictModel, require_aware_datetime
from .location import CityCode


class WeatherSource(StrEnum):
    REAL_API = "REAL_API"
    CACHE = "CACHE"
    FIXED_DEMO = "FIXED_DEMO"


WeatherSourceValue = Annotated[WeatherSource, Field(strict=False)]


class WeatherSnapshot(StrictModel):
    city_code: CityCode
    temperature_c: float = Field(ge=-80, le=80)
    condition: str = Field(min_length=1, max_length=32)
    source: WeatherSourceValue
    fetched_at: datetime
    provider: Literal["OPEN_METEO", "FIXED_DEMO"]

    @field_validator("fetched_at")
    @classmethod
    def validate_fetched_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "fetched_at")
