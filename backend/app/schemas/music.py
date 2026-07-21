"""Fixed emotion-to-music catalog keys for the competition Demo."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field

from .analysis import TextStateLabel
from .step3 import StateLabel


class PlaylistKey(StrEnum):
    RELAX = "RELAX"
    COMFORT = "COMFORT"
    UPLIFT = "UPLIFT"
    COOLDOWN = "COOLDOWN"
    NEUTRAL = "NEUTRAL"


PlaylistKeyValue = Annotated[PlaylistKey, Field(strict=False)]

LOGICAL_TRACK_BY_PLAYLIST: dict[PlaylistKey, str] = {
    PlaylistKey.RELAX: "emotion_relax_01",
    PlaylistKey.COMFORT: "emotion_comfort_01",
    PlaylistKey.UPLIFT: "emotion_uplift_01",
    PlaylistKey.COOLDOWN: "emotion_cooldown_01",
    PlaylistKey.NEUTRAL: "emotion_neutral_01",
}
PLAYLIST_BY_LOGICAL_TRACK = {
    track_id: playlist_key
    for playlist_key, track_id in LOGICAL_TRACK_BY_PLAYLIST.items()
}
# Persisted actions created before the multi-playlist phase remain readable.
PLAYLIST_BY_LOGICAL_TRACK["calm_piano_01"] = PlaylistKey.RELAX

EMOTION_PLAYLIST_MAP: dict[TextStateLabel, PlaylistKey] = {
    TextStateLabel.PHYSICAL_FATIGUE: PlaylistKey.RELAX,
    TextStateLabel.STRESSED: PlaylistKey.RELAX,
    TextStateLabel.ANXIOUS: PlaylistKey.RELAX,
    TextStateLabel.EMOTIONAL_LOW: PlaylistKey.COMFORT,
    TextStateLabel.LONELY: PlaylistKey.COMFORT,
    TextStateLabel.HAPPY: PlaylistKey.UPLIFT,
    TextStateLabel.ANGRY: PlaylistKey.COOLDOWN,
    TextStateLabel.CALM: PlaylistKey.NEUTRAL,
    TextStateLabel.OTHER: PlaylistKey.NEUTRAL,
}

LEGACY_STATE_PLAYLIST_MAP: dict[StateLabel, PlaylistKey] = {
    StateLabel.PHYSICAL_FATIGUE: PlaylistKey.RELAX,
    StateLabel.EMOTIONAL_LOW: PlaylistKey.COMFORT,
    StateLabel.OTHER: PlaylistKey.NEUTRAL,
}


def playlist_for_emotion(label: TextStateLabel) -> PlaylistKey:
    return EMOTION_PLAYLIST_MAP[label]


def playlist_for_legacy_state(label: StateLabel) -> PlaylistKey:
    return LEGACY_STATE_PLAYLIST_MAP[label]


def logical_track_for_playlist(playlist_key: PlaylistKey) -> str:
    return LOGICAL_TRACK_BY_PLAYLIST[playlist_key]


def playlist_for_logical_track(track_id: str) -> PlaylistKey:
    try:
        return PLAYLIST_BY_LOGICAL_TRACK[track_id]
    except KeyError as error:
        raise ValueError("logical music track is not allowlisted") from error
