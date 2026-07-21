from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from scripts.phase5_metrics import (
    AcceptanceReport,
    BenchmarkSample,
    assert_report_safe,
    render_markdown,
    summarize,
    write_report,
)


class Phase5MetricsTests(unittest.TestCase):
    def test_nearest_rank_percentiles_use_all_twenty_attempts(self) -> None:
        samples = [
            BenchmarkSample(
                index=index,
                latency_ms=float(index),
                success=index % 2 == 0,
                outcome="EXPECTED",
            )
            for index in range(1, 21)
        ]
        summary = summarize(
            "SYNTHETIC_INTERFACE",
            "TEST_DOUBLE",
            samples,
            minimum_success_count=10,
        )
        self.assertEqual(summary.count, 20)
        self.assertEqual(summary.success_count, 10)
        self.assertEqual(summary.failure_count, 10)
        self.assertEqual(summary.mean_ms, 10.5)
        self.assertEqual(summary.p50_ms, 10.0)
        self.assertEqual(summary.p95_ms, 19.0)
        self.assertTrue(summary.passed)

    def test_percentiles_are_omitted_below_twenty_samples(self) -> None:
        samples = [
            BenchmarkSample(
                index=index,
                latency_ms=float(index),
                success=True,
                outcome="EXPECTED",
            )
            for index in range(1, 20)
        ]
        summary = summarize(
            "SYNTHETIC_INTERFACE",
            "TEST_DOUBLE",
            samples,
            minimum_success_count=19,
        )
        self.assertIsNone(summary.p50_ms)
        self.assertIsNone(summary.p95_ms)
        self.assertFalse(summary.passed)

        with self.assertRaises(ValidationError):
            summary.model_copy(update={"p95_ms": 19.0}).model_validate(
                summary.model_copy(update={"p95_ms": 19.0}).model_dump()
            )

    def test_reports_are_schema_valid_atomic_and_redacted(self) -> None:
        samples = [
            BenchmarkSample(
                index=index,
                latency_ms=1.0,
                success=True,
                outcome="EXPECTED",
            )
            for index in range(1, 21)
        ]
        benchmark = summarize(
            "SYNTHETIC_INTERFACE", "TEST_DOUBLE", samples, minimum_success_count=20
        )
        report = AcceptanceReport(
            schema_version=1,
            phase="Phase 5",
            generated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            sample_target=20,
            benchmarks=[benchmark],
            e2e=[],
            faults=[],
            recovery=[],
            model_restart_status="NOT_RUN_NO_AUTHORIZATION",
            overall_pass=True,
        )
        markdown = render_markdown(report)
        assert_report_safe(markdown)
        with self.assertRaises(ValueError):
            assert_report_safe("unsafe endpoint https://example.invalid")

        with tempfile.TemporaryDirectory() as temporary:
            json_path = Path(temporary) / "results.json"
            markdown_path = Path(temporary) / "REPORT.md"
            write_report(report, json_path, markdown_path)
            reparsed = AcceptanceReport.model_validate_json(
                json_path.read_text(encoding="utf-8")
            )
            self.assertEqual(reparsed.benchmarks[0].p95_ms, 1.0)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), markdown)
            self.assertFalse(json_path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
