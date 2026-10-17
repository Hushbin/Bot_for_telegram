# Ordering implementation plan

The implementation follows these stages. See ORDERING.md for operation and
ORDERING_VALIDATION.md for executed checks and current restoration status.

1. Add ORDERS and ORDER_NOTIFICATIONS to Oracle and SQLite, with constraints,
   indexes, startup schema checks, repeatable migrations, runtime DML grants, and
   ORDERS_ENABLED=false by default. Preserve the configured 20 MB schema quota.
2. Implement shared transactional store operations: confirmed quotes, immutable
   price/contact snapshots, unique confirmation keys, actor checks, status/version
   guards, inventory deduction on acceptance, cancellation/restocking once, and
   database-time expiry after 24 hours.
3. Wire private Telegram order ID, quantity, and confirmation steps; return from
   registration to the selected product; support text/photo order buttons, /orders,
   /sales, pagination, durable callbacks across restarts and other conversations,
   seller acceptance/decline/completion, and cancellation confirmation.
4. Run a managed background worker with notification leases, bounded batches,
   temporary-error backoff, RetryAfter handling, permanent failure reporting,
   expiry, suppression of stale events, safe shutdown, and crash recovery.
   At-least-once alerts must never duplicate orders or stock movements.
5. Update consent, privacy notices, /mydata, and account erasure. Scrub snapshots,
   cancel open orders, restore eligible stock, retain redacted history, and prevent
   old contact access after re-registration. Extend consistent exports and
   versioned restores to five tables, support old three-table backups, honor
   either party's erasure, suppress restored alerts, and reseed all identities.
6. Validate schema upgrades, shared store contracts, real multi-connection races,
   Telegram wiring with simulated responses, delivery failures and restart safety,
   erasure, old/new recovery formats, and actual disposable Oracle migrations.
   Keep Oracle 19c production migration and a real Telegram journey as rollout gates.

Pending orders reserve no stock. Buyers cancel pending orders; sellers handle
accepted cancellations. Accepted stock is restored once, completed stock is not.
Account erasure cancels open orders and returns stock only to a live seller's live
product. Payment and fulfillment are arranged directly between buyer and seller.
