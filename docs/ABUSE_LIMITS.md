# Durable account limits

Finding 6.5 phase 1 adds admission controls for new listings, new orders and photo
uploads across Telegram and the seller web panel. This protects growth per account;
it does not bound total database/storage use or prevent account farming. Registration,
reads, general edits, global capacity alerts and retention remain follow-up work.

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
