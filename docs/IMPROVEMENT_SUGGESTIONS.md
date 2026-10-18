# Remaining bot improvements

Reviewed: **17 October 2026**, against release `2cff9f0`.
This is a source review, not a new performance or production-load test.
Original section numbers are retained for continuity with earlier discussions.
See [ordering validation](ORDERING_VALIDATION.md) for executed checks.
Suggestion 5.4 was implemented on **18 October 2026**; see
[catalog browsing](CATALOG_PAGINATION.md) for behavior and validation.
Suggestion 5.3 was implemented on the same date; see
[guided product entry](GUIDED_PRODUCT_ENTRY.md).
Suggestion 6.3 is also complete: persistent Telegram photo caching, fallback and
replacement/erasure invalidation were implemented and validated on **17 October
2026**; see [photo delivery](PHOTO_DELIVERY.md). Its obsolete finding was removed
on **18 October 2026**. Other
findings below retain their original review date.

The previous review mixed completed defects with open work and described the
pre-ordering application. Completed logging, conversation wiring, input handling,
privacy/erasure, dependency locking, schema and menu work has been removed from
this list. Ordering now provides the previously missing purchase/contact path.
The following improvements remain open.

| Finding | Remaining work | Current evidence and implementation considerations |
| --- | --- | --- |
| 5.2 | Edit/remove listings and replace photos | The UI offers stock changes and initial photo upload, but no general edit/delete commands or photo replacement. Store `update_product` methods support correction of the latest input, not a complete editing UI. Preserve confirmed order snapshots and ensure erasure and version-specific photo cleanup remain correct. |
| 6.1 | Conversation timeout and restart recovery | `main.py` has no configured conversation timeout or persistence. Saved orders/actions survive restarts, but unfinished registration, product entry and unconfirmed order drafts do not. Choose a deliberate resume or restart policy before adding persistence; preserve confirmation and consent requirements. |
| 6.2 | Database connection reuse | `OracleStore.connect` opens a connection per operation. Handlers already offload blocking work to threads, while updates remain serialized. Measure latency and usage before introducing a bounded pool; preserve transaction boundaries and per-user conversation safety. |
| 6.5 | Abuse limits | There is a 5 MB photo limit, but no per-seller listing cap or application-level per-user write cooldown. Define limits that protect the database quota and storage allowance, including order/notification growth. |
| Deployment readiness | Stronger activation gate | `deploy/activate_release.sh` waits 10 seconds and checks that systemd is active. Separate health jobs check Telegram, Oracle and backup freshness, but activation does not wait for those results. Design a bounded readiness gate that distinguishes candidate failures from external outages before changing automatic rollback. |

## Suggested sequence

1. Define abuse limits and readiness acceptance criteria before wider use.
2. Add listing management to the Telegram UI; the seller panel already provides it.
3. Decide draft timeout/restart behavior; add connection reuse when measurements
   justify it.

Deployment readiness is selected for the next implementation; see the
[implementation plan](DEPLOYMENT_READINESS_PLAN.md). **Five findings remain open**;
planning does not complete a finding.

These are application findings. The infrastructure repository's earlier completed
findings are separate. Avoid reintroducing resolved issues from historical reviews
without checking the current implementation and tests.
