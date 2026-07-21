"""Strict contracts for emotion-driven Step3 reactions and policy suggestions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .base import StrictModel, require_aware_datetime
from .phase4 import WeatherSource, WeatherSourceValue


ReactionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
FollowUpText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
ReactionReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class ReactionTone(StrEnum):
    SUPPORTIVE = "SUPPORTIVE"
    CELEBRATORY = "CELEBRATORY"
    CALMING = "CALMING"
    GROUNDING = "GROUNDING"
    NEUTRAL = "NEUTRAL"


class ACDirection(StrEnum):
    WARMER = "WARMER"
    COOLER = "COOLER"


ReactionToneValue = Annotated[ReactionTone, Field(strict=False)]
ACDirectionValue = Annotated[ACDirection, Field(strict=False)]


class EmotionMatchedMusicSuggestion(StrictModel):
    type: Literal["EMOTION_MATCHED_MUSIC"]


class ACSuggestion(StrictModel):
    type: Literal["AC"]
    direction: ACDirectionValue


ReactionSuggestion = EmotionMatchedMusicSuggestion | ACSuggestion


class LLMReaction(StrictModel):
    """Model advice only. This contract intentionally has no action fields."""

    reply_text: ReactionText
    tone: ReactionToneValue
    follow_up_question: FollowUpText | None = None
    reasons: list[ReactionReason] = Field(min_length=1, max_length=3)
    suggestions: list[ReactionSuggestion] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def require_unique_suggestion_types(self) -> "LLMReaction":
        kinds = [item.type for item in self.suggestions]
        if len(kinds) != len(set(kinds)):
            raise ValueError("reaction suggestion types must be unique")
        return self


class ReactionPhase(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    FINAL = "FINAL"


class PolicySuggestionDecision(StrictModel):
    suggestion_type: Literal["EMOTION_MATCHED_MUSIC", "AC"]
    accepted: bool
    reason_code: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=96,
            pattern=r"^[A-Z][A-Z0-9_]*$",
        ),
    ]


class ACDecisionType(StrEnum):
    HEAT = "HEAT"
    COOL = "COOL"
    OFF = "OFF"
    UNAVAILABLE = "UNAVAILABLE"


ACDecisionTypeValue = Annotated[ACDecisionType, Field(strict=False)]
ConfirmedEmotion = Literal[
    "PHYSICAL_FATIGUE",
    "EMOTIONAL_LOW",
    "HAPPY",
    "STRESSED",
    "ANXIOUS",
    "LONELY",
    "ANGRY",
    "CALM",
    "OTHER",
]


class ACDecision(StrictModel):
    """Deterministic AC decision derived from weather and confirmed emotion."""

    decision: ACDecisionTypeValue
    outdoor_temperature_c: float = Field(ge=-80, le=80)
    weather_source: WeatherSourceValue
    weather_fetched_at: datetime
    confirmed_emotion: ConfirmedEmotion
    base_target_temperature: float | None = Field(default=None, ge=16, le=30)
    emotion_adjustment_c: int = Field(ge=-1, le=1)
    target_temperature: int | None = Field(default=None, ge=16, le=30)
    reason_code: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=96,
            pattern=r"^[A-Z][A-Z0-9_]*$",
        ),
    ]

    @field_validator("weather_fetched_at")
    @classmethod
    def validate_weather_fetched_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "weather_fetched_at")

    @model_validator(mode="after")
    def require_temperatures_only_for_active_decisions(self) -> "ACDecision":
        active = self.decision in {ACDecisionType.HEAT, ACDecisionType.COOL}
        if active and (
            self.base_target_temperature is None or self.target_temperature is None
        ):
            raise ValueError("active AC decision requires base and target temperatures")
        if not active and (
            self.base_target_temperature is not None
            or self.target_temperature is not None
            or self.emotion_adjustment_c != 0
        ):
            raise ValueError("inactive AC decision cannot include temperature adjustment")
        if self.decision is ACDecisionType.UNAVAILABLE and self.weather_source is not WeatherSource.FIXED_DEMO:
            raise ValueError("UNAVAILABLE requires FIXED_DEMO weather")
        return self
