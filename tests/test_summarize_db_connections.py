"""Synthetic journal parsing, statistics, privacy and standalone CLI behavior."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import summarize_db_connections as metrics


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_db_connections.py"
BASE = 1_700_000_000_000_000


def journal(elapsed=10.0, outcome="success", timestamp=BASE, *, prefix="INFO:database:DB_CONNECTION ",
            error_type="DatabaseError", **metadata):
    event = dict(v=1, elapsed_ms=elapsed, outcome=outcome)
    if outcome == "failure":
        event["error_type"] = error_type
    record = dict(MESSAGE=prefix + json.dumps(event), __REALTIME_TIMESTAMP=str(timestamp),
                  _HOSTNAME="bot-test", _BOOT_ID="a" * 32, _PID="123",
                  _SYSTEMD_INVOCATION_ID="b" * 32)
    record.update(metadata)
    return record


def summarize(records):
    return metrics.summarize(json.dumps(record) for record in records)


class ConnectionSummaryTests(unittest.TestCase):
    def test_counts_nearest_rank_percentiles_and_rates_separate_outcomes(self):
        records = [journal(n, timestamp=BASE + n * 1_000_000) for n in range(1, 101)]
        records.extend([journal(1000, "failure", BASE + 101_000_000),
                        journal(2000, "failure", BASE + 121_000_000, error_type="TimeoutError")])
        report = summarize(reversed(records))  # Input ordering must not change the window.
        group = report["groups"][0]
        self.assertEqual(report["records"]["valid_events"], 102)
        self.assertEqual(group["attempts"], 102)
        self.assertEqual(group["observed_span_seconds"], 120)
        self.assertEqual(group["observed_attempts_per_minute"], 51)
        self.assertEqual(group["success"], dict(count=100, mean_ms=50.5,
                                               p50_ms=50, p95_ms=95, p99_ms=99))
        self.assertEqual(group["failure"], dict(count=2, mean_ms=1500,
                                               p50_ms=1000, p95_ms=2000, p99_ms=2000))
        self.assertEqual(group["failure_classes"], dict(DatabaseError=1, TimeoutError=1))
        self.assertTrue(group["first_event_utc"].endswith("+00:00"))

    def test_empty_and_single_timestamp_have_explicit_insufficient_data(self):
        output = io.StringIO()
        with redirect_stdout(output):
            metrics.print_summary(summarize([]))
        self.assertIn("Insufficient data", output.getvalue())
        report = summarize([journal(0), journal(2)])
        group = report["groups"][0]
        self.assertIsNone(group["observed_attempts_per_minute"])
        self.assertEqual(group["success"]["mean_ms"], 1)
        self.assertEqual(group["failure"]["count"], 0)
        self.assertIsNone(group["failure"]["p95_ms"])
        output = io.StringIO()
        with redirect_stdout(output):
            metrics.print_summary(report)
        self.assertIn("one distinct timestamp", output.getvalue())
        self.assertIn("failure: count=0; insufficient data", output.getvalue())

    def test_process_host_boot_and_invocation_groups_are_never_merged(self):
        records = [journal(), journal(_HOSTNAME="other-host"), journal(_PID="456"),
                   journal(_BOOT_ID="c" * 32), journal(_SYSTEMD_INVOCATION_ID="d" * 32)]
        groups = summarize(records)["groups"]
        self.assertEqual(len(groups), 5)
        self.assertTrue(all(g["attempts"] == 1 for g in groups))
        self.assertTrue(all(g["observed_attempts_per_minute"] is None for g in groups))

    def test_optional_invocation_and_both_supported_log_formats(self):
        first = journal(prefix="DB_CONNECTION ")
        second = journal(timestamp=BASE + 60_000_000)
        for record in (first, second):
            del record["_SYSTEMD_INVOCATION_ID"]
        report = summarize([first, second])
        self.assertEqual(len(report["groups"]), 1)
        self.assertEqual(report["groups"][0]["invocation_id"], "unknown")
        self.assertEqual(report["groups"][0]["observed_attempts_per_minute"], 2)

    def test_unrelated_messages_and_invalid_json_are_counted_without_echoing_input(self):
        records = [dict(MESSAGE="private-password"), dict(MESSAGE=[1, 2, 3]), [],
                   journal(prefix="INFO:other:DB_CONNECTION ")]
        report = metrics.summarize([*(json.dumps(record) for record in records), "private-token", " "])
        self.assertEqual(report["records"]["ignored_records"], 5)
        self.assertEqual(report["records"]["invalid_json_lines"], 1)
        self.assertEqual(report["records"]["valid_events"], 0)
        self.assertNotIn("private", json.dumps(report))

    def test_malformed_event_values_and_versions_are_handled_safely(self):
        events = [dict(v=1, outcome="success", elapsed_ms=value)
                  for value in (-1, float("nan"), float("inf"), "10", True, None, 10**1000)]
        events.extend([
            dict(v=1, outcome="failure", elapsed_ms=1),
            dict(v=1, outcome="failure", elapsed_ms=1, error_type="private-password\nsecret"),
            dict(v=1, outcome="success", elapsed_ms=1, sql="private-password"),
            dict(v=1, outcome="unknown", elapsed_ms=1),
            dict(v=True, outcome="success", elapsed_ms=1),
            dict(v="1", outcome="success", elapsed_ms=1), [], {},
        ])
        records = [journal(MESSAGE="DB_CONNECTION " + json.dumps(event)) for event in events]
        records.append(journal(MESSAGE="DB_CONNECTION {private-password}"))
        records.append(journal(MESSAGE='DB_CONNECTION {"v":2}'))
        report = summarize(records)
        self.assertEqual(report["records"]["malformed_events"], len(events) + 1)
        self.assertEqual(report["unsupported_versions"], {"2": 1})
        self.assertEqual(report["groups"], [])
        output = io.StringIO()
        with redirect_stdout(output):
            metrics.print_summary(report)
        self.assertIn("Unsupported event versions", output.getvalue())
        self.assertNotIn("private-password", output.getvalue())

    def test_missing_or_invalid_journal_metadata_is_not_combined_into_an_unknown_group(self):
        records = []
        for key, bad_values in {
            "__REALTIME_TIMESTAMP": (None, "-1", "NaN", "1.5", "9" * 18, True),
            "_HOSTNAME": (None, "host\nprivate-token"),
            "_BOOT_ID": (None, "unknown"), "_PID": (None, "0", "not-a-pid"),
            "_SYSTEMD_INVOCATION_ID": (None, "bad"),
        }.items():
            for value in bad_values:
                records.append(journal(**{key: value}))
        report = summarize(records)
        self.assertEqual(report["records"]["malformed_events"], len(records))
        self.assertEqual(report["groups"], [])
        self.assertNotIn("private-token", json.dumps(report))

    def test_large_finite_values_and_timestamp_endpoints_remain_valid_json(self):
        report = summarize([journal(1e308, timestamp=0),
                            journal(1e308, timestamp=metrics.MAX_TIMESTAMP_US)])
        self.assertEqual(report["groups"][0]["success"]["mean_ms"], 1e308)
        self.assertIn("9999-12-31", report["groups"][0]["last_event_utc"])
        json.dumps(report, allow_nan=False)


class SummaryCLITests(unittest.TestCase):
    def run_cli(self, *args, input_text=""):
        # -S proves that the standalone script needs only the standard library.
        return subprocess.run([sys.executable, "-S", str(SCRIPT), *args], input=input_text,
                              text=True, capture_output=True, timeout=10)

    def test_stdin_structured_output_and_no_app_or_network_dependencies(self):
        result = self.run_cli("--json", input_text=json.dumps(journal()) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        self.assertEqual(report["records"]["valid_events"], 1)
        self.assertIsNone(report["groups"][0]["observed_attempts_per_minute"])

    def test_file_input_human_report_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            content = json.dumps(journal(12.5)) + "\n"
            path.write_text(content, encoding="utf-8")
            path.chmod(0o400)
            result = self.run_cli(str(path))
            self.assertEqual(path.read_text(), content)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Host: bot-test", result.stdout)
        self.assertIn("mean_ms=12.500", result.stdout)

    def test_missing_file_and_invalid_utf8_are_reported_without_exposing_paths_or_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-password"
            missing = self.run_cli(str(path))
            path.write_bytes(b"private-token\xff")
            invalid = self.run_cli(str(path))
        for result in (missing, invalid):
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("Could not read", result.stderr)
            self.assertNotIn("private", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
