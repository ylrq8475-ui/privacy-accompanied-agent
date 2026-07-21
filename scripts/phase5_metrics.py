"""Strict, reproducible metrics and report rendering for Phase 5."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Annotated

from pydantic import Field, JsonValue, field_validator, model_validator

from backend.app.schemas.base import StrictModel, require_aware_datetime


SafeName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class BenchmarkSample(StrictModel):
    index: int = Field(ge=1)
    latency_ms: float = Field(ge=0)
    success: bool
    outcome: SafeName
    error_code: SafeName | None = None

    @field_validator("latency_ms")
    @classmethod
    def finite_latency(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("latency must be finite")
        return round(value, 3)


class BenchmarkSummary(StrictModel):
    interface: SafeName
    implementation: SafeName
    latency_population: str = Field(pattern=r"^ALL_ATTEMPTS$")
    minimum_success_count: int = Field(ge=0)
    count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    min_ms: float | None
    max_ms: float | None
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    passed: bool
    samples: list[BenchmarkSample]

    @model_validator(mode="after")
    def validate_summary(self) -> "BenchmarkSummary":
        if self.count != len(self.samples):
            raise ValueError("count must equal sample count")
        if self.success_count + self.failure_count != self.count:
            raise ValueError("success and failure counts must equal count")
        if self.p95_ms is not None and self.count < 20:
            raise ValueError("P95 requires at least 20 samples")
        if self.count < 20 and (self.p50_ms is not None or self.p95_ms is not None):
            raise ValueError("percentiles require at least 20 samples")
        return self


class E2EResult(StrictModel):
    mode: SafeName
    count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    unique_session_count: int = Field(ge=0)
    passed: bool
    details: dict[str, JsonValue]


class FaultResult(StrictModel):
    scenario: SafeName
    passed: bool
    outcome: SafeName


class RecoveryResult(StrictModel):
    classification: SafeName
    passed: bool
    auto_execution_count: int = Field(ge=0)
    states: dict[str, str]
    console_reloaded: bool
    connector_recreated: bool


class AcceptanceReport(StrictModel):
    schema_version: int = Field(ge=1)
    phase: str = Field(pattern=r"^Phase 5$")
    generated_at: datetime
    sample_target: int = Field(ge=20)
    benchmarks: list[BenchmarkSummary]
    e2e: list[E2EResult]
    faults: list[FaultResult]
    recovery: list[RecoveryResult]
    model_restart_status: str = Field(pattern=r"^NOT_RUN_NO_AUTHORIZATION$")
    overall_pass: bool

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "generated_at")


def nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires samples")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered)) - 1
    return round(ordered[index], 3)


def summarize(
    interface: str,
    implementation: str,
    samples: list[BenchmarkSample],
    *,
    minimum_success_count: int,
) -> BenchmarkSummary:
    latencies = [sample.latency_ms for sample in samples]
    count = len(samples)
    success_count = sum(sample.success for sample in samples)
    percentiles_ready = count >= 20
    return BenchmarkSummary(
        interface=interface,
        implementation=implementation,
        latency_population="ALL_ATTEMPTS",
        minimum_success_count=minimum_success_count,
        count=count,
        success_count=success_count,
        failure_count=count - success_count,
        success_rate=round(success_count / count, 6) if count else 0.0,
        min_ms=round(min(latencies), 3) if latencies else None,
        max_ms=round(max(latencies), 3) if latencies else None,
        mean_ms=round(fmean(latencies), 3) if latencies else None,
        p50_ms=nearest_rank(latencies, 0.50) if percentiles_ready else None,
        p95_ms=nearest_rank(latencies, 0.95) if percentiles_ready else None,
        passed=count >= 20 and success_count >= minimum_success_count,
        samples=samples,
    )


def render_markdown(report: AcceptanceReport) -> str:
    lines = [
        "# Phase 5 Test, Performance, and Recovery Report",
        "",
        f"Generated: `{report.generated_at.isoformat()}`",
        "",
        f"Overall: **{'PASS' if report.overall_pass else 'FAIL'}**",
        "",
        "## Interface benchmarks",
        "",
        "All percentiles use the nearest-rank method over all attempts. A P95 is present only when count is at least 20. MEASURED means availability was observed but success was not required because the Demo must pass its separate degraded E2E gate.",
        "",
        "| Interface | Implementation | Count | Success | Failure | Rate | Min ms | Mean ms | P50 ms | P95 ms | Result |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.benchmarks:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.interface,
                    item.implementation,
                    str(item.count),
                    str(item.success_count),
                    str(item.failure_count),
                    f"{item.success_rate:.1%}",
                    _metric(item.min_ms),
                    _metric(item.mean_ms),
                    _metric(item.p50_ms),
                    _metric(item.p95_ms),
                    (
                        "MEASURED"
                        if item.minimum_success_count == 0 and item.passed
                        else "PASS" if item.passed else "FAIL"
                    ),
                ]
            )
            + " |"
        )
    lines.extend(["", "## End-to-end", ""])
    for item in report.e2e:
        lines.append(
            f"- `{item.mode}`: {item.success_count}/{item.count}, "
            f"unique sessions {item.unique_session_count}, "
            f"{'PASS' if item.passed else 'FAIL'}"
        )
    lines.extend(["", "## Fault matrix", ""])
    for item in report.faults:
        lines.append(
            f"- `{item.scenario}`: {'PASS' if item.passed else 'FAIL'} (`{item.outcome}`)"
        )
    lines.extend(["", "## Recovery", ""])
    for item in report.recovery:
        lines.append(
            f"- `{item.classification}`: {'PASS' if item.passed else 'FAIL'}; "
            f"automatic executions={item.auto_execution_count}; "
            f"console reloaded={str(item.console_reloaded).lower()}; "
            f"connector recreated={str(item.connector_recreated).lower()}"
        )
    lines.extend(
        [
            "",
            "## Model restart",
            "",
            f"`{report.model_restart_status}`",
            "",
            "Real Step3/ASR failures remain failures. AC is a Mock. Recording playback is used only in deterministic E2E, while the Music Executor benchmark uses the real LOCAL miniaudio device.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: AcceptanceReport, json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    markdown = render_markdown(report)
    _atomic_write(json_path, encoded)
    _atomic_write(markdown_path, markdown)


def assert_report_safe(report_text: str) -> None:
    forbidden = (
        r"https?://",
        r"(?:\d{1,3}\.){3}\d{1,3}",
        r"[A-Za-z]:\\",
        r"/home/",
        r"raw_audio",
        r"raw_video",
        r"api[_-]?token",
        r"private[_-]?key",
    )
    for pattern in forbidden:
        if re.search(pattern, report_text, flags=re.IGNORECASE):
            raise ValueError(f"report contains forbidden pattern: {pattern}")


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"
