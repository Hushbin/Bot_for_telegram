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
| 6.2 | Database connection reuse | Connections remain per operation. [Opening-time measurements and a local summarizer](DATABASE_CONNECTION_METRICS.md) were implemented on 18 October 2026; collect representative observations before deciding whether to introduce a bounded pool. Preserve transaction boundaries and per-user conversation safety. |
| 6.5 | Abuse limits | Phase 1 implements durable per-account listing, order and upload budgets; see [account limits](ABUSE_LIMITS.md). Global capacity monitoring, historical-data retention, registration abuse and general request limits remain open. |

## Suggested sequence

1. Roll out the implemented per-account limits in stages; define global capacity and retention controls before wider use.
2. Add listing management to the Telegram UI; the seller panel already provides it.
3. Decide draft timeout/restart behavior; add connection reuse when measurements
   justify it.

The measurement stage of finding 6.2 is implemented; see
[Oracle connection measurements](DATABASE_CONNECTION_METRICS.md) for usage and
validation. Connection and transaction behavior is preserved; pooling requires a
later decision based on observations. The [deployment readiness guide](DEPLOYMENT_READINESS.md)
documents the implemented gate, staged privileged VM setup and outstanding live checks.
**Four findings remain open**; planning and instrumentation alone do not complete
the connection-reuse finding.

These are application findings. The infrastructure repository's earlier completed
findings are separate. Avoid reintroducing resolved issues from historical reviews
without checking the current implementation and tests.
