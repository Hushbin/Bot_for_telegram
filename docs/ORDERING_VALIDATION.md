# Ordering validation

## Verified checks: 17 October 2026

The reconstructed implementation was checked against Oracle **19.33.0.1.0**,
using generated disposable schemas and a temporary runtime identity. That identity
was verified to have no read access to `SME_SCHEMA`; production data was not used
as test fixtures. All generated users and schemas were removed after testing.

- Full suite: **277 tests passed, no skips**, including all 43 Oracle tests.
  The default offline suite also passed (234 passed, 43 Oracle tests skipped).
- Shared order contracts, concurrent inventory acceptance and cancellation,
  transaction rollback, participant isolation, expiry and account erasure passed.
- Telegram purchase/signup/confirmation/action journeys used simulated HTTP.
  Delivery retry, lease, expiry and shutdown tests passed.
- Actual Oracle fresh installation, three-to-five-table migration, repeat setup,
  preservation of fixture data and the unchanged 20 MB schema quota passed.
- Actual Oracle format-2 export/restore preserved accepted orders and stock,
  suppressed restored notifications, and reseeded identities correctly. Erasure
  reconciliation cancelled and scrubbed the erased buyer's order and returned
  accepted stock once. Format-1 restore left both ordering tables empty.
- GitHub Actions test-only run `39037874276` passed on the ordering branch.
  Shell syntax checks and `git diff --check` passed.

## Production rollout verification

The 17 October migration followed a verified final format-1 export. The existing
one user, one seller and one product were preserved (row hashes matched before
and after); the new ordering tables were empty. Runtime SELECT/INSERT/UPDATE
grants were verified, with no DELETE grant. Allocated schema segments remained
0.5 MB under the unchanged 20 MB quota.

For the subsequent format-3 admission-controls release, follow
[the account-limits rollout](ABUSE_LIMITS.md#rollout) and its compatible rollback
baseline. The sequence below records the original ordering rollout.

Follow [the staged rollout](ORDERING.md#enable-after-migration): verify the final
old-release backup before migration, deploy with ordering disabled, verify a
five-table format-2 backup, then enable ordering and check service, Telegram,
database, scheduled jobs and health alarms. Runtime flag and release state should
always be checked on the VM, rather than inferred from this document.

The operator authorized enablement after automated checks and will perform the
real two-account buyer/seller UI check afterward. **That manual journey is not
covered by the automated results above.** Use separate accounts and an agreed
test listing; self-orders are rejected. Check purchase, acceptance and cancellation
and confirm stock restoration. Automated tests send no messages to real users.
