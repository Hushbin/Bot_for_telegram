# Ordering validation

## Previous implementation run

The previous run completed 288 tests with no failures or skips on Python 3.12.14,
including 56 Oracle-specific store, inventory-race, and worker tests. It used a
disposable local Oracle Database Free 23 container, image `gvenzl/oracle-free:23-slim`,
digest `sha256:6d61d267a3b978c24c5ac1790e62e927416a0aec446bd86e4b3a1527562757bd`.

That run also checked actual Oracle fresh installation, repeat installation,
three-to-five-table migration preserving fixture data, runtime DML grants,
format-1 and format-2 restore, erasure reconciliation, and identity reseeding.
The small migrated fixture occupied 1.13 MB under a 20 MB schema quota.

## Workspace recovery

The workspace returned to the published initial checkout and lost the uncommitted
ordering files. The implementation and tests were reconstructed from the saved
conversation and committed on `codex/ordering-recovery`. Upstream deployment
updates are retained. This reconstruction is not a byte-for-byte recovery.

Fresh checks on the restored implementation (17 October 2026):

- `.venv/bin/python -m unittest discover -s tests`: 277 tests run, 234 passed,
  43 Oracle tests skipped because no disposable Oracle connection is configured.
- Shared order contracts, eight actual multiple-connection SQLite inventory races,
  Telegram purchase/signup/confirmation/action journeys with simulated HTTP,
  delivery retries/leases/expiry/shutdown, and five-table recovery with actual
  SQLite foreign keys and rollback all passed.
- `git diff --check`: passed in both application and infrastructure repositories.
- The restored Oracle adapter and migrations have not been rerun against Oracle.
  The previous run above provides historical evidence, not fresh verification of
  these reconstructed files. CI results are not available through this session's
  connectors.

## Deployment checks

The live OCI configuration uses Oracle 19c; the previous Oracle checks used local
Oracle 23. Live migration, actual quota headroom, enabling `ORDERS_ENABLED=true`,
and a real buyer/seller Telegram journey remain rollout checks. Automated Telegram
tests use simulated API responses and do not send messages to real users.
