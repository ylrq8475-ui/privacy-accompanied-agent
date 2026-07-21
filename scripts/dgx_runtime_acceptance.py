"""Synthetic runtime acceptance for the deployed DGX backend.

The script never plays returned audio, authorizes actions, or persists raw text.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_WAV_BYTES = 2 * 1024 * 1024
SYNTHETIC_TEXTS = (
    "今天完成了一个合成测试任务，我感到很开心。",
    "这是部署验收使用的合成文本，我现在比较平静。",
    "Synthetic deployment check: I feel calm after finishing the task.",
    "这是虚构场景：今天任务较多，我感觉有些压力。",
)

WARMER_EMOTIONS = {"PHYSICAL_FATIGUE", "EMOTIONAL_LOW", "LONELY"}
COOLER_EMOTIONS = {"STRESSED", "ANXIOUS", "ANGRY"}


@dataclass(frozen=True, slots=True)
class HTTPResult:
    status: int
    content_type: str
    body: bytes
    latency_ms: float


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> HTTPResult:
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "*/*"},
        )
        started = time.perf_counter_ns()
        try:
            with self.opener.open(request, timeout=150) as response:
                raw = response.read(MAX_JSON_BYTES + 1)
                status = int(response.status)
                content_type = response.headers.get_content_type()
        except urllib.error.HTTPError as error:
            raw = error.read(MAX_JSON_BYTES + 1)
            status = int(error.code)
            content_type = error.headers.get_content_type()
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        if len(raw) > MAX_JSON_BYTES:
            raise RuntimeError("acceptance response exceeded 2 MiB")
        return HTTPResult(status, content_type, raw, latency_ms)


def decode_json(result: HTTPResult) -> dict[str, Any]:
    if result.content_type != "application/json":
        raise RuntimeError("expected application/json")
    value = json.loads(result.body.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("JSON response must be an object")
    return value


def metrics(samples: list[float], successes: int, errors: dict[str, int]) -> dict[str, Any]:
    ordered = sorted(samples)
    result: dict[str, Any] = {
        "count": len(samples),
        "success_count": successes,
        "failure_count": len(samples) - successes,
        "success_rate": round(successes / len(samples), 4) if samples else 0.0,
        "errors": dict(sorted(errors.items())),
    }
    if samples:
        result.update(
            {
                "min_ms": round(min(samples), 3),
                "max_ms": round(max(samples), 3),
                "mean_ms": round(statistics.fmean(samples), 3),
                "p50_ms": round(statistics.median(samples), 3),
            }
        )
    if len(ordered) >= 20:
        index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999999) - 1))
        result["p95_ms"] = round(ordered[index], 3)
    return result


def record_error(errors: dict[str, int], result: HTTPResult) -> None:
    code = f"HTTP_{result.status}"
    if result.content_type == "application/json":
        try:
            body = decode_json(result)
            if isinstance(body.get("error"), str):
                code = str(body["error"])
        except (RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    errors[code] = errors.get(code, 0) + 1


def verify_ac_decision(
    weather: dict[str, Any], emotion: str, decision: dict[str, Any]
) -> dict[str, Any]:
    """Recalculate the deterministic policy without authorizing its action."""

    source = str(weather.get("source", "MISSING"))
    if source == "FIXED_DEMO":
        expected_decision = "UNAVAILABLE"
        expected_target = None
        expected_adjustment = 0
    elif source in {"REAL_API", "CACHE"}:
        temperature = Decimal(str(weather.get("temperature_c")))
        expected_adjustment = (
            1 if emotion in WARMER_EMOTIONS else -1 if emotion in COOLER_EMOTIONS else 0
        )
        if temperature < Decimal("15"):
            expected_decision = "HEAT"
            base = Decimal("22") + (Decimal("15") - temperature) / Decimal("6")
            base = min(Decimal("25"), max(Decimal("22"), base))
            expected_target = int(
                (base + expected_adjustment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        elif temperature <= Decimal("26"):
            expected_decision = "OFF"
            expected_target = None
            expected_adjustment = 0
        else:
            expected_decision = "COOL"
            base = Decimal("27") - (temperature - Decimal("26")) / Decimal("6")
            base = min(Decimal("27"), max(Decimal("24"), base))
            expected_target = int(
                (base + expected_adjustment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
    else:
        return {
            "verified": False,
            "error": f"unsupported weather source: {source}",
        }

    actual = {
        "decision": decision.get("decision"),
        "target_temperature": decision.get("target_temperature"),
        "emotion_adjustment_c": decision.get("emotion_adjustment_c"),
    }
    expected = {
        "decision": expected_decision,
        "target_temperature": expected_target,
        "emotion_adjustment_c": expected_adjustment,
    }
    return {
        "verified": actual == expected,
        "weather_source": source,
        "outdoor_temperature_c": weather.get("temperature_c"),
        "confirmed_emotion": emotion,
        "expected": expected,
        "actual": actual,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if args.samples < 20:
        parser.error("--samples must be at least 20 before reporting P95")

    client = Client(args.base_url)
    analysis_latencies: list[float] = []
    analysis_successes = 0
    analysis_errors: dict[str, int] = {}
    weather_sources: dict[str, int] = {}
    reusable: dict[str, Any] | None = None

    for index in range(args.samples):
        result = client.request(
            "POST", "/v1/analysis/text", {"text": SYNTHETIC_TEXTS[index % len(SYNTHETIC_TEXTS)]}
        )
        analysis_latencies.append(result.latency_ms)
        if result.status != 200:
            record_error(analysis_errors, result)
            continue
        body = decode_json(result)
        source = str((body.get("weather_context") or {}).get("source", "MISSING"))
        weather_sources[source] = weather_sources.get(source, 0) + 1
        if not isinstance(body.get("analysis_id"), str):
            analysis_errors["ANALYSIS_ID_MISSING"] = analysis_errors.get(
                "ANALYSIS_ID_MISSING", 0
            ) + 1
            continue
        analysis_successes += 1
        reusable = body

    tts_latencies: list[float] = []
    tts_successes = 0
    tts_errors: dict[str, int] = {}
    session_id: str | None = None
    ac_verification: dict[str, Any] = {
        "verified": False,
        "error": "session setup did not complete",
    }
    if reusable is not None:
        continued = client.request(
            "POST",
            f"/v1/analysis/text/{reusable['analysis_id']}/sessions",
            {},
        )
        if continued.status == 201:
            session = decode_json(continued)
            session_id = str(session.get("session_id", "")) or None
            primary = reusable.get("primary_state") or {}
            label = primary.get("label")
            if session_id and isinstance(label, str):
                confirmed = client.request(
                    "POST",
                    f"/v1/analysis/sessions/{session_id}/state-confirmation",
                    {"label": label},
                )
                if confirmed.status != 200:
                    session_id = None
                else:
                    confirmed_body = decode_json(confirmed)
                    weather = confirmed_body.get("weather_context")
                    decision = confirmed_body.get("ac_decision")
                    if isinstance(weather, dict) and isinstance(decision, dict):
                        ac_verification = verify_ac_decision(weather, label, decision)
                    else:
                        ac_verification = {
                            "verified": False,
                            "error": "weather_context or ac_decision missing",
                        }

    for _ in range(args.samples):
        if session_id is None:
            tts_latencies.append(0.0)
            tts_errors["SESSION_SETUP_FAILED"] = tts_errors.get(
                "SESSION_SETUP_FAILED", 0
            ) + 1
            continue
        result = client.request(
            "POST", f"/v1/analysis/sessions/{session_id}/tts", {}
        )
        tts_latencies.append(result.latency_ms)
        if (
            result.status == 200
            and result.content_type in {"audio/wav", "audio/x-wav"}
            and 44 <= len(result.body) <= MAX_WAV_BYTES
            and result.body[:4] == b"RIFF"
            and result.body[8:12] == b"WAVE"
        ):
            tts_successes += 1
        else:
            record_error(tts_errors, result)

    if session_id is not None:
        client.request("POST", f"/v1/analysis/sessions/{session_id}/reset")
    client.request("DELETE", "/v1/emotion-summaries")

    analysis_result = metrics(
        analysis_latencies, analysis_successes, analysis_errors
    )
    tts_result = metrics(tts_latencies, tts_successes, tts_errors)
    passed = (
        analysis_successes == args.samples
        and tts_successes == args.samples
        and ac_verification.get("verified") is True
    )
    report = {
        "status": "PASS" if passed else "FAIL",
        "synthetic_inputs_only": True,
        "physical_action_authorized": False,
        "audio_playback_invoked": False,
        "analysis_and_real_step3": analysis_result,
        "weather_sources": dict(sorted(weather_sources.items())),
        "deterministic_ac_decision": ac_verification,
        "stepaudio_tts": tts_result,
        "audius": {
            "status": "NOT_CONFIGURED",
            "real_fetch_samples": 0,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
