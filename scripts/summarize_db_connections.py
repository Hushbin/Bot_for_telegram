"""Summarize Oracle connection-opening events from journalctl -o json; no network access."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from statistics import mean
import sys


PREFIXES = ("DB_CONNECTION ", "INFO:database:DB_CONNECTION ")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_TIMESTAMP_US = 253402300799999999  # datetime.max in journal microseconds


def _utc(timestamp_us):
    return (EPOCH + timedelta(microseconds=timestamp_us)).isoformat()


def _identity(record):
    host, boot, pid = (record.get(key) for key in ("_HOSTNAME", "_BOOT_ID", "_PID"))
    invocation = record.get("_SYSTEMD_INVOCATION_ID", "unknown")
    if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", host)
            or not isinstance(boot, str) or not re.fullmatch(r"[0-9a-f]{32}", boot)
            or not isinstance(pid, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", pid)
            or not isinstance(invocation, str)
            or (invocation != "unknown" and not re.fullmatch(r"[0-9a-f]{32}", invocation))):
        raise ValueError
    return host, boot, pid, invocation


def _timestamp(record):
    raw = record.get("__REALTIME_TIMESTAMP")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,18}", raw):
        raise ValueError
    value = int(raw)
    if value > MAX_TIMESTAMP_US:
        raise ValueError
    return value


def _samples(values):
    ordered = sorted(values)
    result = dict(count=len(ordered), mean_ms=None, p50_ms=None, p95_ms=None, p99_ms=None)
    if ordered:
        result["mean_ms"] = mean(ordered)
        for label, percentile in (("p50_ms", 50), ("p95_ms", 95), ("p99_ms", 99)):
            # Integer nearest rank, including when there is only one sample.
            result[label] = ordered[(percentile * len(ordered) + 99) // 100 - 1]
    return result


def summarize(lines):
    """Return statistics without reproducing raw messages or malformed input."""
    counts = dict(input_lines=0, invalid_json_lines=0, ignored_records=0,
                  malformed_events=0, valid_events=0)
    unsupported = Counter()
    groups = {}
    for line in lines:
        counts["input_lines"] += 1
        if not line.strip():
            counts["ignored_records"] += 1
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            counts["invalid_json_lines"] += 1
            continue
        message = record.get("MESSAGE") if isinstance(record, dict) else None
        prefix = next((p for p in PREFIXES if isinstance(message, str) and message.startswith(p)), None)
        if prefix is None:
            counts["ignored_records"] += 1
            continue
        try:
            event = json.loads(message[len(prefix):])
            if not isinstance(event, dict) or type(event.get("v")) is not int or event["v"] < 1:
                raise ValueError
            if event["v"] != 1:
                unsupported[str(event["v"])] += 1
                continue
            outcome = event.get("outcome")
            elapsed = event.get("elapsed_ms")
            if (outcome not in ("success", "failure") or type(elapsed) not in (int, float)
                    or not math.isfinite(elapsed) or elapsed < 0):
                raise ValueError
            expected_keys = {"v", "outcome", "elapsed_ms"}
            if outcome == "failure":
                expected_keys.add("error_type")
                error_type = event.get("error_type")
                if not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type):
                    raise ValueError
            if set(event) != expected_keys:
                raise ValueError
            identity, timestamp = _identity(record), _timestamp(record)
        except (ValueError, TypeError, OverflowError, RecursionError):
            counts["malformed_events"] += 1
            continue
        counts["valid_events"] += 1
        group = groups.setdefault(identity, dict(success=[], failure=[], errors=Counter(),
                                                first=timestamp, last=timestamp))
        group[outcome].append(elapsed)
        group["first"] = min(group["first"], timestamp)
        group["last"] = max(group["last"], timestamp)
        if outcome == "failure":
            group["errors"][event["error_type"]] += 1

    summaries = []
    for (host, boot, pid, invocation), group in sorted(groups.items()):
        total = len(group["success"]) + len(group["failure"])
        span_us = group["last"] - group["first"]
        summaries.append(dict(
            host=host, boot_id=boot, pid=pid, invocation_id=invocation, attempts=total,
            first_event_utc=_utc(group["first"]), last_event_utc=_utc(group["last"]),
            observed_span_seconds=span_us / 1_000_000,
            observed_attempts_per_minute=total * 60_000_000 / span_us if span_us else None,
            success=_samples(group["success"]), failure=_samples(group["failure"]),
            failure_classes=dict(sorted(group["errors"].items())),
        ))
    return dict(event_version=1, records=counts, unsupported_versions=dict(sorted(unsupported.items())),
                groups=summaries)


def print_summary(report):
    counts = report["records"]
    print("Oracle connection opening (event v1; milliseconds)")
    print(f"Valid events: {counts['valid_events']}; malformed events: {counts['malformed_events']}; "
          f"invalid JSON lines: {counts['invalid_json_lines']}; ignored records: {counts['ignored_records']}")
    if report["unsupported_versions"]:
        print("Unsupported event versions: " + json.dumps(report["unsupported_versions"], sort_keys=True))
    if not report["groups"]:
        print("Insufficient data: no valid connection events.")
    for group in report["groups"]:
        print(f"\nHost: {group['host']}; boot: {group['boot_id']}; PID: {group['pid']}; "
              f"invocation: {group['invocation_id']}")
        print(f"Observed window (UTC): {group['first_event_utc']} to {group['last_event_utc']}")
        print(f"Attempts: {group['attempts']}")
        rate = group["observed_attempts_per_minute"]
        print("Observed attempts/minute: " + (f"{rate:.3f}" if rate is not None else
                                             "insufficient data (one distinct timestamp)"))
        for outcome in ("success", "failure"):
            stats = group[outcome]
            if not stats["count"]:
                print(f"{outcome}: count=0; insufficient data for latency statistics")
            else:
                print(f"{outcome}: count={stats['count']} " + " ".join(
                    f"{name}={stats[name]:.3f}" for name in ("mean_ms", "p50_ms", "p95_ms", "p99_ms")))
        print("Failure classes: " + json.dumps(group["failure_classes"], sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="journal JSON-lines file; - for stdin (default)")
    parser.add_argument("--json", action="store_true", help="output structured JSON instead of text")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            report = summarize(sys.stdin)
        else:
            with Path(args.input).open(encoding="utf-8") as source:
                report = summarize(source)
    except (OSError, UnicodeError):
        # File contents and paths may be sensitive; don't echo an input or exception.
        print("Could not read the input as UTF-8 journal JSON lines.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    else:
        print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
