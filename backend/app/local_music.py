"""Authorization-aware LOCAL playback for the single Phase 4 Demo track."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Protocol

from backend.app.mocks import ActionMockError
from backend.app.schemas.actions import (
    ActionAuthorization,
    ActionProposal,
    ActionResult,
    ActionType,
    AuthorizationStatus,
    ExecutionStatus,
    MusicActionPayload,
)
from backend.app.schemas.network import MusicPayload
from backend.app.schemas.music import playlist_for_logical_track


DEFAULT_MUSIC_ROOT = Path("data/music")
ALLOWED_TRACK_ID = "calm_piano_01"


class LocalMusicError(ActionMockError):
    pass


class PlaybackBackend(Protocol):
    def play(self, path: Path) -> None:
        """Open the audio device and begin background playback."""

    def play_memory(self, audio: bytes) -> None:
        """Open the audio device and begin playback from in-memory bytes."""

    def close(self) -> None:
        """Stop playback and release the device."""


class MiniaudioPlaybackBackend:
    """Lazy miniaudio wrapper so importing the ASGI app never opens a device."""

    def __init__(self) -> None:
        self._device = None
        self._stream = None
        self._audio: bytes | None = None

    def play(self, path: Path) -> None:
        try:
            import miniaudio
        except ImportError as error:
            raise LocalMusicError("miniaudio is not installed") from error
        self.close()
        try:
            stream = miniaudio.stream_file(str(path))
            device = miniaudio.PlaybackDevice()
            device.start(stream)
        except Exception as error:
            try:
                if "device" in locals():
                    device.close()
            except Exception:
                pass
            raise LocalMusicError("local audio playback could not start") from error
        self._stream = stream
        self._device = device

    def play_memory(self, audio: bytes) -> None:
        try:
            import miniaudio
        except ImportError as error:
            raise LocalMusicError("miniaudio is not installed") from error
        self.close()
        try:
            stream = miniaudio.stream_memory(audio)
            device = miniaudio.PlaybackDevice()
            device.start(stream)
        except Exception as error:
            try:
                if "device" in locals():
                    device.close()
            except Exception:
                pass
            raise LocalMusicError("memory audio playback could not start") from error
        self._audio = audio
        self._stream = stream
        self._device = device

    def close(self) -> None:
        device = self._device
        self._device = None
        self._stream = None
        self._audio = None
        if device is None:
            return
        try:
            device.stop()
        finally:
            device.close()


class LocalMusicPlayer:
    def __init__(
        self,
        root: str | Path = DEFAULT_MUSIC_ROOT,
        *,
        backend: PlaybackBackend | None = None,
    ) -> None:
        self.root = Path(root)
        self.backend = backend or MiniaudioPlaybackBackend()
        self.executed_action_ids: list[str] = []
        self._lock = RLock()

    def health(self) -> dict[str, object]:
        try:
            path = self._validated_track(ALLOWED_TRACK_ID)
        except LocalMusicError:
            return {"component": "LOCAL_MUSIC", "available": False, "status": "ASSET_INVALID", "latency_ms": 0}
        return {
            "component": "LOCAL_MUSIC",
            "available": True,
            "status": "PLAYING" if self.executed_action_ids else "READY_NOT_PLAYED",
            "latency_ms": 0,
            "track": path.name,
        }

    def execute(
        self,
        proposal: ActionProposal,
        authorization: ActionAuthorization,
        now: datetime,
        *,
        fallback_reason: str = "NOT_CONFIGURED",
        fetch_invoked: bool = False,
    ) -> ActionResult:
        with self._lock:
            self._validate_action(proposal, authorization, now)

            command = MusicPayload(action="play", track_id=proposal.payload.track_id)
            playlist_key = playlist_for_logical_track(command.track_id)
            path = self._validated_track(ALLOWED_TRACK_ID)
            try:
                self.backend.play(path)
            except LocalMusicError:
                raise
            except Exception as error:
                raise LocalMusicError("local audio playback could not start") from error
            self.executed_action_ids.append(proposal.action_id)
            return ActionResult(
                action_id=proposal.action_id,
                action_type=proposal.action_type,
                execution_status=ExecutionStatus.SUCCEEDED,
                result={
                    "mock": False,
                    "playback_started": True,
                    "physical_action_performed": True,
                    "track_id": command.track_id,
                    "playlist_key": playlist_key.value,
                    "fallback_asset_id": ALLOWED_TRACK_ID,
                    "network_scope": "LOCAL",
                    "source": "LOCAL_FALLBACK",
                    "provider": "LOCAL",
                    "fetch_scope": "INTERNET" if fetch_invoked else "NOT_INVOKED",
                    "playback_scope": "LOCAL",
                    "fallback_used": True,
                    "fallback_reason": fallback_reason,
                    "fallback_notice": (
                        "EMOTION_PLAYLIST_UNAVAILABLE_USING_LOCAL_CALM_PIANO"
                    ),
                    "preview": False,
                },
                completed_at=now,
            )

    def execute_preview(
        self,
        proposal: ActionProposal,
        authorization: ActionAuthorization,
        now: datetime,
        *,
        audio: bytes,
        provider_track_id: str,
        size_bytes: int,
        fetch_latency_ms: int,
    ) -> ActionResult:
        with self._lock:
            self._validate_action(proposal, authorization, now)
            playlist_key = playlist_for_logical_track(proposal.payload.track_id)
            try:
                self.backend.play_memory(audio)
            except LocalMusicError:
                raise
            except Exception as error:
                raise LocalMusicError("memory audio playback could not start") from error
            self.executed_action_ids.append(proposal.action_id)
            return ActionResult(
                action_id=proposal.action_id,
                action_type=proposal.action_type,
                execution_status=ExecutionStatus.SUCCEEDED,
                result={
                    "mock": False,
                    "playback_started": True,
                    "physical_action_performed": True,
                    "track_id": proposal.payload.track_id,
                    "playlist_key": playlist_key.value,
                    "provider_track_id": provider_track_id,
                    "network_scope": "LOCAL",
                    "source": "AUDIUS_PREVIEW",
                    "provider": "AUDIUS",
                    "fetch_scope": "INTERNET",
                    "playback_scope": "LOCAL",
                    "fallback_used": False,
                    "fallback_reason": None,
                    "preview": True,
                    "size_bytes": size_bytes,
                    "fetch_latency_ms": fetch_latency_ms,
                },
                completed_at=now,
            )

    def close(self) -> None:
        with self._lock:
            self.backend.close()

    def _validate_action(
        self,
        proposal: ActionProposal,
        authorization: ActionAuthorization,
        now: datetime,
    ) -> None:
        if proposal.action_type is not ActionType.PLAY_MUSIC or not isinstance(
            proposal.payload, MusicActionPayload
        ):
            raise LocalMusicError("local player received a non-music action")
        if proposal.action_id != authorization.action_id:
            raise LocalMusicError("music action_id does not match authorization")
        if proposal.action_type is not authorization.action_type:
            raise LocalMusicError("music action type does not match authorization")
        if authorization.authorization_status is not AuthorizationStatus.APPROVED:
            raise LocalMusicError("music action is not approved")
        if now >= proposal.expires_at or now >= authorization.expires_at:
            raise LocalMusicError("music authorization has expired")
        if proposal.action_id in self.executed_action_ids:
            raise LocalMusicError("music action has already executed")

    def _validated_track(self, track_id: str) -> Path:
        if track_id != ALLOWED_TRACK_ID:
            raise LocalMusicError("track_id is not allowlisted")
        catalog_path = self.root / "catalog.json"
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            tracks = catalog["tracks"]
            track = next(item for item in tracks if item.get("track_id") == track_id)
            relative = Path(track["path"])
            expected_digest = str(track["sha256"]).lower()
        except (OSError, json.JSONDecodeError, KeyError, StopIteration, TypeError) as error:
            raise LocalMusicError("music catalog is invalid") from error
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path.suffix.casefold() != ".flac":
            raise LocalMusicError("music catalog path is unsafe")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise LocalMusicError("music asset is unavailable") from error
        if digest != expected_digest:
            raise LocalMusicError("music asset digest does not match catalog")
        return path
