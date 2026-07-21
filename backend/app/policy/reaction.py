"""Deterministic policy for converting LLM reaction suggestions into proposals."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from backend.app.schemas.actions import ACActionPayload, ACMode
from backend.app.schemas.analysis import TextStateLabel
from backend.app.schemas.music import (
    PlaylistKey,
    logical_track_for_playlist,
    playlist_for_emotion,
)
from backend.app.schemas.phase4 import WeatherSnapshot, WeatherSource
from backend.app.schemas.reaction import (
    ACDecision,
    ACDecisionType,
    ACSuggestion,
    EmotionMatchedMusicSuggestion,
    LLMReaction,
    PolicySuggestionDecision,
)


@dataclass(frozen=True, slots=True)
class ReactionPolicyResult:
    music_track_id: str | None
    playlist_key: PlaylistKey | None
    ac_payload: ACActionPayload | None
    ac_decision: ACDecision
    decisions: tuple[PolicySuggestionDecision, ...]


_POSITIVE_EMOTION_ADJUSTMENTS = {
    TextStateLabel.PHYSICAL_FATIGUE,
    TextStateLabel.EMOTIONAL_LOW,
    TextStateLabel.LONELY,
}
_NEGATIVE_EMOTION_ADJUSTMENTS = {
    TextStateLabel.STRESSED,
    TextStateLabel.ANXIOUS,
    TextStateLabel.ANGRY,
}


def _clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(maximum, value))


def decide_ac(
    weather: WeatherSnapshot,
    confirmed_emotion: TextStateLabel,
) -> tuple[ACDecision, ACActionPayload | None]:
    """Derive an AC decision without using free-form model advice."""

    emotion = TextStateLabel(confirmed_emotion)
    common = {
        "outdoor_temperature_c": weather.temperature_c,
        "weather_source": weather.source,
        "weather_fetched_at": weather.fetched_at,
        "confirmed_emotion": emotion.value,
    }
    if weather.source not in {WeatherSource.REAL_API, WeatherSource.CACHE}:
        return (
            ACDecision(
                decision=ACDecisionType.UNAVAILABLE,
                **common,
                base_target_temperature=None,
                emotion_adjustment_c=0,
                target_temperature=None,
                reason_code="WEATHER_SOURCE_NOT_ELIGIBLE",
            ),
            None,
        )

    temperature = Decimal(str(weather.temperature_c))
    if Decimal("15") <= temperature <= Decimal("26"):
        return (
            ACDecision(
                decision=ACDecisionType.OFF,
                **common,
                base_target_temperature=None,
                emotion_adjustment_c=0,
                target_temperature=None,
                reason_code="OUTDOOR_TEMPERATURE_COMFORTABLE",
            ),
            None,
        )

    if emotion in _POSITIVE_EMOTION_ADJUSTMENTS:
        adjustment = 1
    elif emotion in _NEGATIVE_EMOTION_ADJUSTMENTS:
        adjustment = -1
    else:
        adjustment = 0

    if temperature < Decimal("15"):
        decision_type = ACDecisionType.HEAT
        mode = ACMode.HEAT
        base = _clamp(
            Decimal("22") + (Decimal("15") - temperature) / Decimal("6"),
            Decimal("22"),
            Decimal("25"),
        )
        reason_code = "OUTDOOR_TEMPERATURE_REQUIRES_HEAT"
    else:
        decision_type = ACDecisionType.COOL
        mode = ACMode.COOL
        base = _clamp(
            Decimal("27") - (temperature - Decimal("26")) / Decimal("6"),
            Decimal("24"),
            Decimal("27"),
        )
        reason_code = "OUTDOOR_TEMPERATURE_REQUIRES_COOLING"

    target = int(
        (base + Decimal(adjustment)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    decision = ACDecision(
        decision=decision_type,
        **common,
        base_target_temperature=float(base),
        emotion_adjustment_c=adjustment,
        target_temperature=target,
        reason_code=reason_code,
    )
    return (
        decision,
        ACActionPayload(
            device_id="living_room_ac_mock",
            mode=mode,
            target_temperature=target,
            duration_minutes=30,
        ),
    )


def evaluate_reaction_suggestions(
    reaction: LLMReaction,
    weather: WeatherSnapshot,
    *,
    music_preference: str | None,
    music_preference_confirmed: bool,
    confirmed_emotion: TextStateLabel,
) -> ReactionPolicyResult:
    music_track_id: str | None = None
    playlist_key: PlaylistKey | None = None
    decisions: list[PolicySuggestionDecision] = []

    for suggestion in reaction.suggestions:
        if isinstance(suggestion, EmotionMatchedMusicSuggestion):
            if music_preference_confirmed and music_preference == "NONE":
                decisions.append(
                    PolicySuggestionDecision(
                        suggestion_type="EMOTION_MATCHED_MUSIC",
                        accepted=False,
                        reason_code="MUSIC_PREFERENCE_NONE",
                    )
                )
            else:
                playlist_key = playlist_for_emotion(confirmed_emotion)
                music_track_id = logical_track_for_playlist(playlist_key)
                decisions.append(
                    PolicySuggestionDecision(
                        suggestion_type="EMOTION_MATCHED_MUSIC",
                        accepted=True,
                        reason_code=f"EMOTION_MAPPED_{playlist_key.value}",
                    )
                )
            continue

        # The LLM AC suggestion is retained in the public contract, but deliberately
        # does not influence deterministic action creation.
        if isinstance(suggestion, ACSuggestion):
            continue

    ac_decision, ac_payload = decide_ac(weather, confirmed_emotion)
    decisions.append(
        PolicySuggestionDecision(
            suggestion_type="AC",
            accepted=ac_payload is not None,
            reason_code=ac_decision.reason_code,
        )
    )
    return ReactionPolicyResult(
        music_track_id,
        playlist_key,
        ac_payload,
        ac_decision,
        tuple(decisions),
    )
