"""Strict contracts for experimental, non-persistent text state analysis."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .base import Identifier, ShortText, StrictModel
from .location import CityCode
from .phase4 import WeatherSnapshot
from .reaction import LLMReaction


AnalysisText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class TextStateLabel(StrEnum):
    PHYSICAL_FATIGUE = "PHYSICAL_FATIGUE"
    EMOTIONAL_LOW = "EMOTIONAL_LOW"
    HAPPY = "HAPPY"
    STRESSED = "STRESSED"
    ANXIOUS = "ANXIOUS"
    LONELY = "LONELY"
    ANGRY = "ANGRY"
    CALM = "CALM"
    OTHER = "OTHER"


TextStateLabelValue = Annotated[TextStateLabel, Field(strict=False)]


TEXT_STATE_DISPLAY_NAMES: dict[TextStateLabel, str] = {
    TextStateLabel.PHYSICAL_FATIGUE: "身体疲劳",
    TextStateLabel.EMOTIONAL_LOW: "情绪低落",
    TextStateLabel.HAPPY: "开心",
    TextStateLabel.STRESSED: "压力",
    TextStateLabel.ANXIOUS: "焦虑",
    TextStateLabel.LONELY: "孤独",
    TextStateLabel.ANGRY: "愤怒",
    TextStateLabel.CALM: "平静",
    TextStateLabel.OTHER: "其他",
}


class TextAnalysisRequest(StrictModel):
    text: AnalysisText
    city_code: CityCode = "310000"


class TextStateHypothesis(StrictModel):
    label: TextStateLabelValue
    confidence: float = Field(ge=0, le=1)
    evidence: list[ShortText] = Field(default_factory=list, max_length=3)


class TextStateModelOutput(StrictModel):
    state_hypotheses: list[TextStateHypothesis] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def require_unique_labels(self) -> "TextStateModelOutput":
        labels = [item.label for item in self.state_hypotheses]
        if len(labels) != len(set(labels)):
            raise ValueError("state hypothesis labels must be unique")
        return self


class TextStateResult(StrictModel):
    label: TextStateLabelValue
    display_name: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[ShortText] = Field(default_factory=list, max_length=3)


class TextAnalysisResponse(StrictModel):
    analysis_id: Identifier
    experimental: Literal[True] = True
    model_source: Literal["STEP3"] = "STEP3"
    confidence_kind: Literal["MODEL_SELF_REPORTED_UNCALIBRATED"] = (
        "MODEL_SELF_REPORTED_UNCALIBRATED"
    )
    primary_state: TextStateResult
    candidates: list[TextStateResult] = Field(min_length=1, max_length=5)
    reaction: LLMReaction
    weather_context: WeatherSnapshot
    latency_ms: int = Field(ge=0)


def build_text_analysis_response(
    analysis_id: str,
    output: TextStateModelOutput,
    reaction: LLMReaction,
    weather_context: WeatherSnapshot,
    latency_ms: int,
) -> TextAnalysisResponse:
    ordered = sorted(
        enumerate(output.state_hypotheses),
        key=lambda item: (-item[1].confidence, item[0]),
    )
    candidates = [
        TextStateResult(
            label=hypothesis.label,
            display_name=TEXT_STATE_DISPLAY_NAMES[hypothesis.label],
            confidence=hypothesis.confidence,
            evidence=hypothesis.evidence,
        )
        for _, hypothesis in ordered
    ]
    return TextAnalysisResponse(
        analysis_id=analysis_id,
        primary_state=candidates[0],
        candidates=candidates,
        reaction=reaction,
        weather_context=weather_context,
        latency_ms=latency_ms,
    )
