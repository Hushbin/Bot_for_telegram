# Oracle connection measurements

`OracleStore.connect` records the time spent opening each Oracle connection using
a monotonic clock. It returns the original driver connection and preserves the
original exception on failure. Connections remain per operation; this change
does not introduce a pool, retry, extra query, or transaction/lifecycle change.
SQLite and direct administrative connections in setup/restore scripts are unchanged.

## Events

The `database` logger emits compact INFO events in this versioned format:

```text
DB_CONNECTION {"v":1,"elapsed_ms":75.4,"outcome":"success"}
DB_CONNECTION {"v":1,"elapsed_ms":5000.0,"outcome":"failure","error_type":"DatabaseError"}
```

The application's default logging format prepends `INFO:database:`. Events contain
no credentials, DSNs, wallet paths, SQL, bind values, exception messages or user/
product IDs. Failures include only the exception class. Timing and logging are
best effort: instrumentation failures do not discard a successful connection or
replace the driver error. Consequently, a broken logging sink can lose samples.

## Reading measurements

After deploying this version, run on the bot VM:

```sh
sudo journalctl -u smebot.service --since '24 hours ago' --no-pager -o json \
  | /opt/smebot/current/.venv/bin/python \
      /opt/smebot/current/scripts/summarize_db_connections.py
```

The summarizer also accepts a journal JSON-lines file and optional structured
output, and needs only Python 3.10+ with its standard library:

```sh
python3 scripts/summarize_db_connections.py journal.jsonl --json
```

Input must be the JSON output of `journalctl -o json`, including its normal
metadata. Plain text logs are not supported. Both the bare event prefix and the
default `INFO:database:` prefix are recognized. The tool performs no network or
database calls and does not modify its input.

Each report contains:

- Input counts: valid events, malformed measurement events, invalid JSON lines,
  ignored unrelated records and unsupported event versions.
- Separate host, boot ID, PID and systemd invocation groups. Required host/boot/PID
  metadata must be valid. Missing invocation metadata is labeled `unknown`; when
  that occurs, prefilter one execution to avoid PID reuse within the same boot.
- First and last event times in UTC, total attempts, and observed attempts/minute.
  Rate uses the span between those events, not the requested journal query window.
  A single distinct timestamp produces an explicit insufficient-data result.
- Separate success and failure counts, means and nearest-rank p50/p95/p99 opening
  latency in milliseconds, plus failure counts by exception class. No samples for
  an outcome means no latency statistics for it.

Malformed records are counted without reproducing their contents. Empty input is
reported as insufficient data, not as evidence of a healthy database. Small
samples can make tail percentiles equal the maximum; keep the sample count with
any reported percentile. Exact percentiles retain latency samples in memory, so
process bounded journal windows when reviewing a large history.

## Using the results

Compare opening latency and attempt frequency with existing whole-operation/photo
lookup timings during a representative traffic window. This helps establish
whether connection creation contributes materially to latency and whether a
later pool would be worth its complexity. Exception classes identify categories
of failure without disclosing driver messages.

These measurements exclude SQL time, transaction duration, thread-queue time and
connection closing. They do not measure peak concurrent sessions or determine
pool size. They include startup and worker activity when those run in the bot
service, so attempt rate is not user request throughput. Do not average per-process
percentiles to obtain an overall percentile, and do not generate production
orders or listing writes solely to populate a report.

Finding 6.2 remains open pending observations and a separate pooling decision.
No schema migration, new dependency, configuration or service-unit change is
required. Deployment uses the normal application workflow and rollback path.

## OCI Monitoring follow-up

This implementation reads local journals only. OCI publication and alarms are
a possible subsequent step: reuse the existing instance-principal publisher and
`smebot_health` namespace, publish from a bounded background exporter, and keep
monitoring failures independent of database operations. Decide aggregation,
delivery guarantees and low-cardinality dimensions separately; local process IDs
and invocation IDs need not become metric dimensions. Establish a baseline before
choosing alert thresholds.

## Validation

On **18 October 2026**, `.venv/bin/python -m unittest discover -s tests -v`
ran 404 tests: **352 passed and 52 optional Oracle tests were skipped** because
no throwaway Oracle database was configured. The 17 instrumentation and summary
tests cover return/exception identity, connection arguments, log and clock
failures, secret-bearing driver errors, transaction commit/rollback/cleanup,
grouping, percentile/rate calculations, insufficient data, malformed and
unsupported events, timestamp/duration boundaries, and standalone read-only CLI
behavior without third-party dependencies. Existing application tests also passed.

Live Oracle checks and a production observation window have not been performed
for this change. Measurement-specific automated tests use mock driver connections
and synthetic journals; they do not contact Oracle or use production records as
write fixtures.
