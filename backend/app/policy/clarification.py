"""Deterministic clarification policy; model prose never controls this decision."""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.schemas.step3 import StateHypothesis


@dataclass(frozen=True, slots=True)
class ClarificationDecision:
    required: bool
    top_confidence: float
    confidence_gap: float | None
    reason_codes: tuple[str, ...]


def evaluate_clarification(
    hypotheses: list[StateHypothesis],
    *,
    minimum_confidence: float = 0.70,
    minimum_gap: float = 0.25,
) -> ClarificationDecision:
    """Apply the Phase 1B thresholds to structured confidence values only."""

    if not hypotheses:
        raise ValueError("at least one state hypothesis is required")
    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum_confidence must be between 0 and 1")
    if not 0.0 <= minimum_gap <= 1.0:
        raise ValueError("minimum_gap must be between 0 and 1")

    ranked = sorted((item.confidence for item in hypotheses), reverse=True)
    top_confidence = ranked[0]
    confidence_gap = top_confidence - ranked[1] if len(ranked) > 1 else None
    reasons: list[str] = []
    if top_confidence < minimum_confidence:
        reasons.append("TOP_CONFIDENCE_BELOW_THRESHOLD")
    if confidence_gap is not None and confidence_gap < minimum_gap:
        reasons.append("TOP_TWO_GAP_BELOW_THRESHOLD")
    return ClarificationDecision(
        required=bool(reasons),
        top_confidence=top_confidence,
        confidence_gap=confidence_gap,
        reason_codes=tuple(reasons),
    )
