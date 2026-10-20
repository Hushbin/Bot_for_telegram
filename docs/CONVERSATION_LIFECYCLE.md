# Conversation inactivity and restart policy

Implemented for finding **6.1** on **20 October 2026**.

Private multi-step flows expire after **30 minutes of inactivity in that flow**.
This includes registration, profile and seller details, preferences, guided and
compact product entry, photo selection, stock input, product management,
erasure confirmation and unconfirmed orders. Valid input, validation retries and
current-step hints renew the deadline. New entry commands start a fresh flow.

The latest-message correction window also expires after 30 minutes of inactivity,
including after a successful save. This clears retained contact drafts and saved
product correction references. A correction before expiry renews that window.
Older/unknown edited messages stay ignored and do not prolong the window.

`/products`, `/myproducts`, `/orders`, `/sales`, `/privacy`, `/mydata`, catalog page
buttons and saved-order actions remain usable during a flow and do not renew its
deadline. Menu actions keep their existing behavior of replacing the current flow.
Stale product-entry/management buttons and saved-confirmation replays do not
extend another draft's deadline.

## Cleanup and next interaction

A process-local asyncio task sweeps every 30 seconds without sending Telegram
messages. It removes only the application's flow fields from `user_data`,
including draft personal data, pending product/photo references and latest-input
metadata. Cleanup can lag the deadline by one sweep interval; callback guards
check the exact deadline before interpreting input, so the lag cannot admit a
late save, photo or confirmation.

On the next interaction the bot explains that the step expired and removes a
pending phone keyboard. Send `/menu` to start again. Fresh entry commands and
buttons still work immediately. Expired text is never reused as the first input
of a new flow. An unknown edited message stays silent and leaves the notice for
a later interaction.

PTB owns its conversation state. The sweeper does not inspect or mutate private
framework state: the next participating callback returns `ConversationHandler.END`
for a stale flow. Until then only a small expiry marker and the old framework
state remain; the draft fields have already been cleared. Unrelated user-data
fields are preserved.

Already admitted saves and photo workers are allowed to finish, including
upload/attach compensation. Cleanup skips busy callbacks, and any remaining step
gets a new deadline after completion. Expiry does not delete saved profiles,
products, photos, orders or abuse counters. Saved product entry followed by an
expired optional-photo step still leaves the product available.

## Restart and saved orders

Drafts and correction context have no persistence. Restarting the bot discards
them. Unhandled new private input gets `/menu` guidance and removes a stale phone
keyboard; unknown edits are ignored. Start a new flow rather than attempting to
resume an old prompt. Stateful old buttons cannot save a missing draft.

Saved orders and their actions continue to use database authorization, status and
version checks. Confirmation handling checks the durable idempotency key first:
a replay displays the saved order even after expiry or restart. If no order was
saved, expired confirmation cannot create one; the buyer must review a new summary
and confirm again. No new schema, dependencies, persistence backend, JobQueue or
runtime settings are required.

## Validation

Fake-clock tests feed synthetic updates through the real Telegram dispatcher and
SQLite, with Telegram HTTP replaced. They cover deadline boundaries, independent
users, background cleanup, keyboard removal, read-only actions, correction
expiry, stale callbacks, erasure/preferences, guided saves, management edits and
removal, photo input, busy saves/uploads and compensation, restart guidance,
consent/idempotency and sweeper shutdown.

On **20 October 2026**, `.venv/bin/python -m unittest discover -s tests -v`
ran **554 tests in 31.505 seconds: 469 passed and 85 optional Oracle tests
skipped**. This includes **29 new lifecycle tests**. `git diff --check` also
passed. No production credentials or live Oracle database were used.

Live Telegram UI checks and production deployment are not performed as part of
this local implementation. Before rollout, exercise a registration, an expired
order review and a saved-order replay with a test bot; confirm the phone keyboard
is removed on the next interaction after expiry.
