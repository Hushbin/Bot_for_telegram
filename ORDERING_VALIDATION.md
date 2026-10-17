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

The workspace subsequently returned to the published initial checkout, losing the
uncommitted ordering files. The implementation is being restored from the saved
conversation and will be committed separately. Fresh validation results for the
restored files will be recorded here before the implementation commit is published.
The deployment updates already on upstream `master` are retained.

## Deployment checks

The live OCI configuration uses Oracle 19c; the previous Oracle checks used local
Oracle 23. Live migration, actual quota headroom, enabling `ORDERS_ENABLED=true`,
and a real buyer/seller Telegram journey remain rollout checks. Automated Telegram
tests use simulated API responses and do not send messages to real users.
