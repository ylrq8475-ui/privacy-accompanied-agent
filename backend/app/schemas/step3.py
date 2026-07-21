"""Strict structured output accepted from a future Step3 adapter."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from .base import Identifier, LongText, ShortText, StrictModel


class StateLabel(StrEnum):
    PHYSICAL_FATIGUE = "PHYSICAL_FATIGUE"
    EMOTIONAL_LOW = "EMOTIONAL_LOW"
    OTHER = "OTHER"


class RecommendationType(StrEnum):
    SUGGEST_MUSIC = "SUGGEST_MUSIC"
    SUGGEST_AC = "SUGGEST_AC"
    NO_ACTION = "NO_ACTION"


StateLabelValue = Annotated[StateLabel, Field(strict=False)]
RecommendationTypeValue = Annotated[RecommendationType, Field(strict=False)]


class StateHypothesis(StrictModel):
    label: StateLabelValue
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[ShortText] = Field(default_factory=list, max_length=8)


class RecommendationCandidate(StrictModel):
    type: RecommendationTypeValue
    category: Identifier | None = None

    @model_validator(mode="after")
    def validate_category(self) -> "RecommendationCandidate":
        if self.type is RecommendationType.SUGGEST_MUSIC and self.category is None:
            raise ValueError("music recommendations require a category")
        if self.type is RecommendationType.NO_ACTION and self.category is not None:
            raise ValueError("NO_ACTION cannot include a category")
        return self


class RecommendationReason(StrictModel):
    code: Identifier
    text: LongText


class ClarificationCandidate(StrictModel):
    question_id: Identifier
    question: LongText
    target_labels: list[StateLabelValue] = Field(min_length=1, max_length=3)


class Step3Output(StrictModel):
    state_hypotheses: list[StateHypothesis] = Field(min_length=1, max_length=5)
    recommended_action: RecommendationCandidate
    recommendation_reason: list[RecommendationReason] = Field(min_length=1, max_length=8)
    clarification_candidates: list[ClarificationCandidate] = Field(default_factory=list, max_length=4)
