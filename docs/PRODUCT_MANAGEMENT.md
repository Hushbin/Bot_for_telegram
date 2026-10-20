# Telegram product management

Implemented on **19 October 2026** for finding 5.2. Telegram and the seller panel
use the same catalog, order protection and media service. No schema migration,
new runtime setting or recovery-format change is needed (format 3 remains current).

## Seller journey

Use `/manageproduct 1` for your first listing, or `/manageproduct G123` for an
owned catalog ID. Without an argument the bot asks for the reference. **Manage**
buttons in `/myproducts` and your own `/product` view open the same menu. Removed
listings do not cause seller numbers to be reused; pagination is preserved.

Choose **Name**, **Category**, **Price**, **Stock** or **Description**, enter a
replacement, then review **Current** and **New** before pressing **Save**.
Category uses buttons; Description also offers **Clear description**. Prices are
USD with at most two decimal places, stock is a nonnegative integer, names are
1–160 characters and descriptions at most 1,000. **Back** abandons the edit and
refreshes the listing. **Cancel** or `/cancel` discards the unfinished flow.
Editing your latest text input updates the review and expires its previous Save
button; editing an older message cannot change the draft or a saved product.

Example: `/manageproduct 1` → **Price** → `24.99` → review `19.99` / `24.99` →
**Save**. Only the price changes. If that field changed elsewhere while the
review was open, saving is rejected and the menu refreshes. Updates to other
fields are preserved. Stock edits retain the accepted-order return-capacity
guard and reject a changed stock value. Existing order name/price/contact
snapshots remain unchanged.

**Add photo** or **Replace photo** asks for one Telegram photo up to 5 MiB.
A valid image is saved immediately; there is no second binary draft/preview.
`/skip` or Back preserves the current photo. Shared daily/cooldown upload budgets
apply before download; an admitted failed attempt consumes its allowance.
Replacement compares both the original object key and version, so a concurrent
panel upload wins rather than being silently overwritten. `/photo` remains an
initial-upload shortcut for listings without a photo.

**Remove product** shows the current full listing and asks for a separate
**Confirm removal**. Changes after that review require a fresh confirmation.
Removal is blocked by any ACCEPTED order or PENDING order whose deadline has
not elapsed. Resolve these in `/sales` first. An elapsed pending request does
not block removal even before the expiry worker runs; removal itself does not
cancel requests, change order status or send notifications. Order history stays
available. Products are hidden by soft deletion and their Telegram photo caches
are cleared. There is no undo action in the bot.

## Media cleanup and recovery

Both interfaces upload, attach and compensate in one synchronous worker. Database
transactions end before Object Storage operations. Failed attachment attempts
delete the new object/version; successful replacements delete the old one.
Cancelling the awaiting request does not interrupt the worker midway through
upload/attachment/compensation. Object keys are private and are not displayed.

Removal attempts to delete the product's exact stored photo version promptly.
The hidden row retains its key/version until deletion and a conditional metadata
clear succeed. A storage outage does not reverse a successful logical removal:
`product_media` logs the product ID and failure category for operator follow-up.
Retrying the authenticated DELETE for the same owned product retries cleanup;
an operator can likewise call the shared `product_media.remove_product` service
for that owner and ID. Telegram does not expose removed products for retry.
Unknown legacy versions retain their metadata and emit a warning: inspect bucket
versions before clearing references. Replacement/orphan cleanup failures are
logged and require operator inspection of the product's private object prefix.
No background cleanup queue is introduced.

Prompt deletion is deliberate: an older backup may refer to photo bytes that
have since been deleted. Restoring that backup cannot recreate those bytes.
Code rollback also cannot undo an edit or removal. The previous format-3 release
can read this release's data, but rolling back restores its older removal rules
and loses the new Telegram management UI.

Management drafts remain in memory and expire after 30 minutes of inactivity in
the flow. Restarting discards them; use
`/manageproduct` again. Buttons bind the actor, nonce and bot message; stale,
forwarded or malformed callbacks cannot mutate products or discard another
active draft. Expired Save/removal buttons and pending photo input cannot change
saved products. Already admitted uploads and their compensation finish normally.
See [conversation lifecycle](CONVERSATION_LIFECYCLE.md) for cleanup and restart behavior.

## Validation

The default suite on **19 October 2026** ran **525 tests: 440 passed and 85
optional Oracle tests skipped**. Automated checks cover real Telegram dispatcher journeys,
field validation/review/corrections, ownership and erased accounts, stale
callbacks/restarts, lost success replies, stock/order snapshots, pending/accepted
removal guards, expired deadlines, concurrent edits and order creation, photo
revision conflicts, cleanup retries and upload budgets. API regressions cover
the same removal policy and exact-version cleanup without changing its JSON
response shape. See the implementation PR for final test totals, isolated Oracle
results and the normal GitHub deployment receipt.

Deployment uses branch → PR checks → master → GitHub CI activation. Verify the
VM release matches the merged commit, all readiness checks pass, the bot PID is
stable, health metrics are healthy and export/health timers remain enabled.
No production products or orders are automated write fixtures. A real seller's
manual Telegram edit/photo/remove journey remains a separate acceptance check.
