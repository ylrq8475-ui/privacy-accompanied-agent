"""Strict, action-free Step3-VL perception contract for Demo scenes."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field

from .base import ShortText, StrictModel


class SceneType(StrEnum):
    INDOOR = "INDOOR"
    OUTDOOR = "OUTDOOR"
    UNCERTAIN = "UNCERTAIN"


SceneTypeValue = Annotated[SceneType, Field(strict=False)]


class VisualPerceptionOutput(StrictModel):
    """Bounded visual facts only; never authorization, identity, or emotion."""

    person_present: bool
    scene_type: SceneTypeValue
    scene_summary: ShortText
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[ShortText] = Field(default_factory=list, max_length=4)
