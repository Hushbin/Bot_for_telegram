# Account and request limits

Finding 6.5 phase 1 adds admission controls for new listings, new orders and photo
uploads across Telegram and the seller web panel. This protects growth per account;
it does not bound total database/storage use or prevent account farming. The
[Telegram request gate](#telegram-request-throttling) also throttles registration,
reads and edits before handler work. Global capacity alerts, historical-data
retention and distributed registration/account-farming controls remain follow-up work.

## Telegram request throttling

The dispatcher admits at most ten immediate updates per Telegram user, refilling
one request per second. It covers private messages (including contacts/photos),
edits and button callbacks, plus non-private callbacks and recognized bot commands.
Unrelated group messages and commands addressed to another bot in a group are
ignored. All covered traffic for an account shares a budget across chats.

| Setting | Code/local default | Behavior |
| --- | --- | --- |
| `TELEGRAM_RATE_LIMIT_ENABLED` | `false` | Enable process-local request admission independently of `ABUSE_LIMITS_ENABLED` |
| `TELEGRAM_RATE_LIMIT_BURST` | `10` | Initial and maximum normal tokens per user |
| `TELEGRAM_RATE_LIMIT_REFILL_PER_SECOND` | `1` | Normal tokens restored per second |

Enabled must be `true` or `false` (case-insensitive); numeric values must be
integers from 1 to 1,000, even when disabled. Invalid settings fail startup.
The versioned `deploy/runtime.env` enables the 10/1 policy for the next deployed
release; local examples leave it disabled. This file takes precedence over the
VM's instance configuration under systemd.

When the normal budget is empty, private `/cancel`, `/privacy`, `/mydata`,
`/deleteme` and the exact erasure-confirmation buttons use a separate reserve:
three requests, replenishing one every ten seconds. Correct command mentions
(such as `/privacy@YourBot`) work; edited commands and other buttons cannot use
the reserve. Normal tokens are always used first. Authentication, ownership,
confirmation and order version checks still belong to the existing handlers.
Cancelling an order through an order button uses the normal budget.

Rejected updates are discarded, not queued or automatically retried. The gate
runs before conversation expiry observation and order callbacks; it performs no
database/media work and does not alter drafts, renew their deadlines, consume
durable write budgets or apply edits. The existing background cleanup can still
expire an inactive draft normally. An admitted request costs a token even if
validation or the handler later fails.

Private messages receive `Too many requests. Try again in N seconds.` at most
once per user every ten seconds. Button rejections are acknowledged exactly once,
with retry text subject to that same feedback interval and otherwise silently.
Non-private messages are rejected silently. A feedback failure does not admit
the request or trigger a second generic error message. Once the budget refills,
retry the action manually; existing order confirmation keys preserve idempotency.

Only Telegram IDs, request balances and monotonic refill/feedback times are
retained in memory. The limiter tracks at most 10,000 users. Idle entries are
removed lazily on covered traffic after `max(60, burst/refill_rate, 30)` seconds;
expiry never grants tokens faster than normal refill. Rejected attempts also
count as activity. At capacity, untracked users are temporarily rejected without
evicting active users or sending messages; callbacks still receive a silent
acknowledgement. The privacy reserve is unavailable to untracked users at capacity.
The cap covers the limiter's own map, not framework state or total process memory.
Restart clears these transient balances; erasure does not grant a fresh budget.
Nothing is added to database exports or retained request history.

During covered traffic, the `telegram_rate_limit` logger emits at most one
`TELEGRAM_RATE_LIMIT` JSON event per minute with protocol `v=1` and aggregate
`accepted`, `rejected`, `reserve_used`, `capacity_rejected` counts since its
previous report (or startup). The traffic triggering a report is included in it.
No report is sent during inactivity, so an unreported final partial window is
lost on restart. Logs contain no IDs, message contents, callback data or tokens;
logging failure cannot change admission. These are local logs, not OCI metrics.

After a requested release through the normal deployment/readiness workflow,
review these counts for unexpected rejection levels. To disable, ship a release
with `TELEGRAM_RATE_LIMIT_ENABLED=false`; durable admission remains independent.
No schema migration, new dependency or second poller is required. This is per-user
protection, not a global traffic limit or a defense against distributed accounts.

## Policy and configuration

`deploy/runtime.env` versions the release policy. `/etc/smebot/bot.env` retains
secrets and instance settings; the release file takes precedence under systemd.

| Setting | Default | Behavior |
| --- | --- | --- |
| `ABUSE_LIMITS_ENABLED` | `false` | Observation records successful admissions; `true` enforces all budgets |
| `ABUSE_ACTIVE_LISTINGS` | 50 | Non-deleted listings per seller, including zero stock |
| `ABUSE_LISTINGS_DAILY` | 10 | New listings per seller per UTC day |
| `ABUSE_LISTING_COOLDOWN` | 10 | Seconds between new listings |
| `ABUSE_ORDERS_DAILY` | 20 | New orders per buyer per UTC day |
| `ABUSE_ORDER_COOLDOWN` | 30 | Seconds between new orders |
| `ABUSE_PENDING_ORDERS` | 10 | Unexpired pending orders per buyer |
| `ABUSE_PENDING_PER_SELLER` | 3 | Unexpired pending orders with one seller |
| `ABUSE_PHOTOS_DAILY` | 20 | Admitted upload attempts per seller per UTC day |
| `ABUSE_PHOTO_COOLDOWN` | 10 | Seconds between admitted upload attempts |

The code default is observation mode. The versioned release policy now sets
`ABUSE_LIMITS_ENABLED=true`, following the verified observation deployment.
Disabling enforcement still records admissions and preserves recovery blocks.

Numeric environment settings must be integers from 1 to 1,000,000. Settings are
validated at startup. All daily counters and cooldowns reset at midnight UTC.
Cancellation, deletion, decline and expiry do not refund daily budgets. Accepted
orders and pending orders whose expiry time has elapsed do not count toward the
pending caps. Existing listings above the cap remain available; creation resumes
only after the seller has fewer than the cap.

Example: a seller who creates ten listings sees “Daily listing limit reached” on
an eleventh attempt, with the reset time. Removing a listing does not return a daily
slot. An order retry using its original confirmation key returns the original
order without another charge or notification.

Limit errors retain Telegram drafts and pending photo state. The API returns its
existing `error` text plus a stable `code`: `listing_daily`, `order_daily`,
`photo_daily`, the corresponding `_cooldown` codes, or `restore_block` produce HTTP
429 and `Retry-After` in seconds. `active_listings` and `pending_orders` produce HTTP
409 with an action to resolve the capacity constraint. Profile and stock edits,
removal, existing-order actions, notification delivery and erasure remain available.

## Transactions and data

`ABUSE_LIMITS` stores one reusable row per Telegram account: UTC window start,
three counters, three last-admission timestamps and an optional recovery block end.
It has no request history, contact data or object keys. Account erasure and
re-registration retain the current-day ledger. Lazy rollover and the worker's
idempotent sweep clear expired counters/timestamps, including for erased accounts.
`/privacy` explains this retention; `/mydata` displays the current-day ledger,
including after profile erasure.

Oracle locks the existing account row before products/orders and ledger changes;
SQLite acquires `BEGIN IMMEDIATE`. Listing admission and order admission commit
with their successful write and notification enqueue. Failed validation or a failed
transaction does not consume a slot. Both listing entry points share validation
and admission. Photo authentication, ownership, revision and known-size checks run
before admission; admission commits before fetching/streaming, decoding or storing
bytes. Later failure consumes the attempt. Attach revalidation and orphan cleanup
still run, without holding a database transaction during external I/O.

Observation has no historical backfill. Counts start when the observation release
runs; enabling enforcement keeps those counts. Unknown past uploads and local
listing timestamps are not reconstructed. Observation does not disable recovery
blocks.

## Migration and backup compatibility

Run `scripts/setup_db.py` before application activation. The additive migration
creates `ABUSE_LIMITS` and grants runtime SELECT, INSERT and UPDATE, without DELETE.
Old application code can continue using its five tables during migration.

New exports use **format 3**, with six tables. Readers retain explicit format 1
(three-table) and format 2 (five-table) schemas. Readiness accepts formats 1, 2 and 3.
Restore still requires an empty six-table destination and honors erasure markers.
Before committing a restore from any format, the importer clears expired admission
metadata, creates missing ledgers
and blocks new listings, orders and uploads for **every restored account until the
next midnight UTC**. This prevents a second daily budget after losing admissions
newer than the snapshot. Existing order actions remain available. A synthesized
ledger is included in the returned import counts.

Older application releases cannot read format 3 and their readiness checks reject
it. Once `latest.json` references format 3, use the observation release as the
rollback baseline; do not reactivate a pre-format-3 release against that pointer.
Keep a known format-2 manifest for operator recovery with the appropriate reader.
Do not remove the ledger table or reset counters when disabling enforcement.

## Rollout

1. Rehearse five-to-six-table migration twice, concurrency contracts and all three
   restore formats in an isolated Oracle owner/runtime pair with no live-schema
   privileges. Remove disposable identities afterward.
2. Pause `smebot-export.timer`, finish any running export and retain a fresh format-2
   manifest using the current release. Run the additive production migration.
3. Merge the observation release through GitHub CI. Confirm release identity,
   process/dependency readiness, schema and observation configuration.
4. Produce and validate a format-3 export. Retain the observation release as the
   compatible rollback baseline, then resume the export timer.
5. Enable `ABUSE_LIMITS_ENABLED=true` in a separate release through GitHub CI.
   Confirm GitHub, local and VM commits match, inspect the process flag and run
   readiness checks. Do not create production fixture orders or inject live faults.

## Validation

Automated Telegram request-throttling validation on **29 October 2026**:

- Python 3.12 offline suite: **581 tests ran, 496 passed and 85 optional Oracle
  tests skipped**, using `.venv/bin/python -m unittest discover -s tests -v`.
- All 27 new policy, limiter and dispatcher tests passed with fake clocks,
  simulated Telegram transport and temporary SQLite/media stores. Coverage includes
  exact refill boundaries (including frequent fractional-second activity), user
  isolation, reserve eligibility/refill, command mentions, feedback suppression and
  failures, bounded storage/expiry, no database/photo work on rejection, unchanged
  drafts/deadlines/durable counters, expiry and idempotent order-confirmation retry.
- Changed Python files passed Python 3.10 syntax parsing; release/example settings,
  shell syntax and `git diff --check` passed. Python 3.10 CI results follow below.

These local checks used synthetic updates and did not contact Oracle or send
real Telegram messages. They are separate from the subsequent deployment below.

### Telegram release verification, 29 October 2026

[PR #6](https://github.com/Hushbin/Bot_for_telegram/pull/6) published implementation
commit `13bf67991703c7261672ecba35fcd90bba50086a` and merged it as
`829f0a7d25f4d88694f99d51ebd52a462311c4a2` on `master`.

- [Pull-request CI](https://github.com/Hushbin/Bot_for_telegram/actions/runs/40954350408)
  passed on Python 3.10.22 before merge; its deployment job was skipped as intended.
- [Release CI and deployment](https://github.com/Hushbin/Bot_for_telegram/actions/runs/40954661718)
  passed both test and deploy jobs for the merged SHA. Each CI test job ran
  **581 tests, with 496 passed and 85 optional Oracle tests skipped**.
- The previous release `b025719b98a190107cd051880fd7a4e1e49075d7` passed all five
  baseline probes before switching. The candidate passed configuration, Telegram,
  database, backup and seller-API probes and the process readiness gate.
  At **21:11:11 UTC**, the logs reported `candidate: ready` and
  `Activated 829f0a7d25f4d88694f99d51ebd52a462311c4a2`. No rollback was required.
- The Bastion transfer/activation step completed successfully, including its
  checked session deletion and restoration of the approved idle allowlist.
  The overall workflow completed successfully at **21:11:36 UTC**.
- The activated release contains `TELEGRAM_RATE_LIMIT_ENABLED=true`, burst `10`
  and refill `1` in its versioned runtime settings. Its Git tree matches the
  locally reviewed implementation. No schema or root-owned VM helper change
  was required.

This verification uses GitHub workflow and VM activation/probe logs. No separate
SSH process-environment inspection, live throttle/flood test, traffic-baseline
measurement or buyer/seller acceptance journey was performed. This Markdown-only
record does not deploy another release.

Automated chat/API
checks simulate user traffic; a real buyer/seller Telegram acceptance journey is a
separate manual check. Per-account budgets do not replace global quota monitoring.

Automated checks on **19 October 2026**:

- Offline suite: 477 tests run, 408 passed and 69 optional Oracle tests skipped
  using Python 3.13 and the pinned dependencies. GitHub CI uses Python 3.10.
- Shared SQLite admission, concurrent listing/order/photo attempts, transaction
  rollback, midnight/cooldown boundaries, restart, erasure/re-registration and
  observation-to-enforcement behavior passed.
- Simulated Telegram draft retries and seller API status/code/retry headers,
  upload rejection, failed attempts and orphan compensation passed.
- Isolated Oracle five-to-six-table migration and repeat migration preserved a
  fixture. Its runtime identity could not read the live schema or DELETE the
  ledger. Format 1/2/3 restores, identity reseeding, recovery blocks and existing
  order actions passed. The Oracle-enabled regression snapshot ran **472 tests,
  all passed with no skips**. Seven final targeted Oracle assertions also passed,
  covering subsequent concurrency, revision, rollover and rollback additions. All
  disposable schemas/runtime identities were removed.
- The final erasure-notice update passed all nine privacy tests. GitHub Python
  3.10 PR checks passed in run `39313010597`; the merged observation release
  passed test and deployment jobs in run `39314228991`.

No production fixture orders or live fault injection were performed. A real
buyer/seller Telegram journey has not been performed by these automated checks.

Live observation verification on **19 October 2026**:

- A fresh format-2 backup passed checksum, column and row-count validation before
  the additive production migration. The six-table runtime check passed; ledger
  grants are SELECT/INSERT/UPDATE only and the schema quota remains 20 MB.
- Observation release `c0bdc83547d97febc64616745a25564c09396a62` was deployed through
  [PR #3](https://github.com/Hushbin/Bot_for_telegram/pull/3) and normal GitHub CI.
  All 91 tracked files matched the commit. The process had zero restarts, stable
  readiness identity and the expected policy values with enforcement disabled.
- Configuration, Telegram, database, backup and seller-API readiness checks passed.
  A fresh format-3 backup then passed checksum and six-table row-count validation;
  readiness also passed against that new backup. Scheduled exports resumed.
- Retain `c0bdc83547d97febc64616745a25564c09396a62` as the format-3-compatible
  observation fallback. The subsequent enforcement release changes the feature
  flag only; numeric limits and persisted counters remain in place. Verify its
  GitHub/local/VM identity and process flag after CI activation as described above.
