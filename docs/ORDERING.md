# Ordering: usage and rollout

An order requests one product and quantity from one seller. The seller receives
an alert containing the buyer's explicitly confirmed name and phone, plus
**Accept** and **Decline** buttons. Payment and fulfillment are arranged directly.

## Buyer and seller flow

1. Open `/product <catalog ID>` and tap **Order**, or send `/order <catalog ID>`.
   New users finish registration and return to the selected product.
2. Enter a quantity and review the seller, product, unit price, total, and exact
   contact details to be shared. Tap **Confirm order**.
3. Buyers find orders in `/orders`; sellers find incoming requests in `/sales`.
   Pending orders reserve no stock and expire after 24 hours. Acceptance checks
   current stock and deducts the quantity atomically.
4. Buyers can cancel pending orders. Sellers can decline pending orders, mark
   accepted orders fulfilled, or confirm an accepted cancellation. Cancellation
   restores accepted stock exactly once. Confirmed prices survive catalog edits.

The bot retries temporary notification failures and records permanent failures
in order details. Histories remain authoritative. A crash after Telegram accepts
an alert can cause a duplicate message with the same order ID, but never a second
order or stock deduction. Saved order actions survive restarts; unconfirmed drafts
need fresh review. Self-orders are rejected.

## Enable after migration

`ORDERS_ENABLED=false` is the default. It blocks new purchases while preserving
existing order histories, actions, expiry, delivery, erasure, and recovery.
For local development, set `APP_BACKEND=local`, a separate test `BOT_TOKEN`, and
`ORDERS_ENABLED=true` in your ignored `.env`. SQLite upgrades automatically.

For Oracle deployment:

1. Rehearse on a disposable schema. Stop the live bot and export/health timers
   for a short maintenance window; take a final export with the old release.
2. Run `.venv/bin/python scripts/setup_db.py` using the existing secure setup
   configuration against the intended database. It adds `ORDERS` and
   `ORDER_NOTIFICATIONS`, indexes, and runtime DML grants, preserving existing data.
3. Deploy the complete release with new ordering disabled. Verify catalog
   behavior and a format-2 export before resuming export jobs.
4. Set `ORDERS_ENABLED=true`, restart, and verify the purchase/accept/cancel flow
   using separate buyer/seller accounts and an agreed test listing. Resume timers.

The schema owner's configured 20 MB quota is unchanged. Check live headroom as
the schema owner before enabling:

```sql
SELECT ROUND(SUM(bytes) / 1024 / 1024, 2) AS segment_mb FROM user_segments;
SELECT tablespace_name, bytes, max_bytes FROM user_ts_quotas;
```

Stock edits must leave room to return accepted quantities within the existing
stock-column limit. New orders can be paused with `ORDERS_ENABLED=false` without
losing existing order resolution. A pre-order release lacks five-table recovery
and order erasure support, so reverting to it requires handling existing orders.

## Privacy and recovery

Each order confirmation authorizes sharing its displayed buyer name and phone
with that seller. Email and seller private contact details are not shared.
`/mydata`, `/orders`, and `/sales` expose stored snapshots and consent information
to the appropriate participants.

Account erasure cancels open orders and scrubs that party's contact or listing
snapshots. Accepted stock returns only to live products of live sellers. Redacted
order IDs, participant IDs, quantities, prices, statuses, versions and timestamps
remain for history and inventory consistency. Re-registration cannot reveal old
erased contacts. Telegram messages already delivered, including a send racing
erasure, cannot reliably be recalled.

Format-2 exports include all five tables in one consistent Oracle snapshot.
Restore also accepts format-1 backups, leaving order tables empty. Format-2
restore honors erasure of either party, keeps scrubbed foreign-key tombstones,
reconciles stock, and reseeds product, order, and notification identities.
Restored unsent notifications are suppressed to prevent replaying old contacts.
Keep the old bot stopped while restoring; new erasure markers during import cause
rollback. Review open orders, stock, redaction, and post-snapshot activity before
switching the live DSN. Activity after the selected snapshot may be missing.

## Tests

Run `.venv/bin/python -m unittest discover -s tests -v` from the repository root.
Telegram API responses in automated tests are simulated. Oracle checks require
`ORACLE_TEST_DSN`, `ORACLE_TEST_USER`, `ORACLE_TEST_PASSWORD`, optional wallet/schema
settings, and `ORACLE_TEST_THROWAWAY=yes`. Those checks write and erase fixture
data; select an explicitly disposable database, never the live bot schema.
