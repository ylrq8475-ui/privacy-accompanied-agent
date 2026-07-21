"""Low-frequency camera/video person observation with synthetic fallback."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .contracts import AdapterError, AdapterHealth, PerceptionSource, VisionObservation


Observer = Callable[[], tuple[bool, bytes | None]]


class VisionAdapter:
    def __init__(
        self,
        camera_index: int = 0,
        video_path: Path | None = None,
        *,
        observers: list[tuple[PerceptionSource, Observer]] | None = None,
    ) -> None:
        self.camera_index = camera_index
        self.video_path = video_path
        self._observers = observers or self._production_observers()
        self._selected = 0
        self._fallback_reasons: list[str] = []

    def health(self) -> AdapterHealth:
        try:
            observation = self.observe()
        except AdapterError as error:
            return AdapterHealth("VISION", False, error.code, 0)
        return AdapterHealth(
            "VISION", True, observation.source.value, observation.latency_ms
        )

    def observe(self) -> VisionObservation:
        started = time.monotonic()
        failures: list[str] = []
        for index in range(self._selected, len(self._observers)):
            source, observer = self._observers[index]
            try:
                present, jpeg = observer()
            except AdapterError as error:
                failures.append(error.code)
                if error.code not in self._fallback_reasons:
                    self._fallback_reasons.append(error.code)
                continue
            self._selected = index
            return VisionObservation(
                person_present=present,
                source=source,
                degraded=source is not PerceptionSource.CAMERA,
                degraded_reasons=tuple(dict.fromkeys(self._fallback_reasons + failures)),
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                jpeg=jpeg,
            )
        raise AdapterError("VISION_UNAVAILABLE", "no Vision source is available")

    def _production_observers(self) -> list[tuple[PerceptionSource, Observer]]:
        observers: list[tuple[PerceptionSource, Observer]] = [
            (PerceptionSource.CAMERA, self._opencv_observer(self.camera_index))
        ]
        if self.video_path is not None:
            observers.append((PerceptionSource.VIDEO, self._opencv_observer(str(self.video_path))))
        observers.append((PerceptionSource.STATIC_SYNTHETIC, lambda: (True, None)))
        return observers

    def close(self) -> None:
        for _, observer in self._observers:
            close = getattr(observer, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _opencv_observer(source: int | str) -> Observer:
        capture = None
        hog = None

        def observe() -> tuple[bool, bytes | None]:
            nonlocal capture, hog
            try:
                import cv2  # type: ignore[import-not-found]
            except ImportError as error:
                raise AdapterError("OPENCV_UNAVAILABLE", "OpenCV is not installed") from error
            if capture is None:
                capture = cv2.VideoCapture(source)
                if not capture.isOpened():
                    capture.release()
                    capture = None
                    raise AdapterError("VISION_SOURCE_UNAVAILABLE", "Vision source could not be opened")
            ok, frame = capture.read()
            if not ok:
                capture.release()
                capture = None
                raise AdapterError("VISION_FRAME_UNAVAILABLE", "Vision source did not return a frame")
            if hog is None:
                hog = cv2.HOGDescriptor()
                hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            height, width = frame.shape[:2]
            scale = min(1.0, 512 / max(height, width))
            resized = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
            boxes, _ = hog.detectMultiScale(resized, winStride=(8, 8), padding=(8, 8), scale=1.05)
            encoded_ok, encoded = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            jpeg = encoded.tobytes() if encoded_ok and len(encoded) <= 524_288 else None
            return bool(len(boxes)), jpeg

        def close() -> None:
            nonlocal capture
            if capture is not None:
                capture.release()
                capture = None

        setattr(observe, "close", close)

        return observe
