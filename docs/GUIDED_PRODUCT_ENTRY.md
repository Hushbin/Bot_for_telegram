# Guided product entry

Registered sellers can use `/addproduct` or the Add product menu button to enter
a name, choose a category button, enter a USD price and stock, and provide a
description. `/skip` at the description step leaves it empty. Each field is
validated separately; no listing is written during these steps.

The review shows every field with **Save product**, **Restart entry** and
**Cancel** buttons. Save rechecks the seller and validates the fields before
creating the listing. Restart discards the draft and starts with the name;
Cancel discards it and ends entry. Saving shows the seller number and catalog ID,
then asks for an optional photo. `/skip` finishes without a photo. Cancelling or
restarting entry after a successful save leaves that listing available.

At the first prompt, the compact shortcut still works:

```text
Blue shirt | clothing | 19.99 | 5 | Cotton shirt
```

A first response containing `|` uses that format and saves immediately, as before.
Other text starts guided entry. Category buttons display labels and store their
stable keys. Prices are stored in USD cents, using decimal arithmetic.

## Validation and corrections

| Field | Accepted range |
| --- | --- |
| Name | 1–160 characters after trimming |
| Category | A key from `catalog.CATEGORIES`, chosen with a button in guided entry |
| Price | Nonnegative, finite, expressible in cents, at most USD 9999999999.99 |
| Stock | Nonnegative whole number, at most 9999999999 |
| Description | At most 1000 characters after trimming; empty or skipped becomes `None` |

Before guided Save, editing the latest text revalidates that field and repeats
its next prompt. An invalid edit returns to that field. Selecting a category or
sending another field makes older messages ineligible. Editing the description
at review refreshes the summary and expires its old buttons. Use Restart entry
or `/addproduct` to change earlier fields. After guided Save, use the seller
panel for changes. Compact input retains its existing latest-message correction
behavior, including correction of the saved row without creating another listing.

## Navigation, failures and restarts

Drafts live only in memory. A bot restart loses unfinished entry; old buttons
direct the seller to `/addproduct`. `/cancel` and new entry commands clear the
draft. Category and review buttons are bound to the current user's draft, a
random revision token and the expected prompt message. Stale, repeated, malformed,
cross-user and group-chat callbacks do not save or change a draft.

Catalog commands and pagination can be used during entry without clearing the
draft. A new order conversation replaces it; expired entry buttons leave an
active order conversation alone.

Successful IDs are recorded and the draft is invalidated before sending the
Telegram success reply. Repeating a known successful Save cannot create another
listing, even when that reply fails. Photo retries use the already saved ID.

A database save failure is not retried automatically. The bot retains the fields
and issues a fresh review, with a notice to check `/myproducts` before retrying:
an uncertain commit may already have created the listing. There is no durable
idempotency key, so this does not guarantee exactly-once creation across uncertain
commits or retries after restart.

No schema migration, configuration change or new dependency is required.

## Verification

On **18 October 2026**, `.venv/bin/python -m unittest discover -s tests -v`
ran 387 tests: **335 passed and 52 optional Oracle tests were skipped** because
no throwaway Oracle database was configured. The 22 focused product-entry tests
cover field boundaries, review/save behavior, omitted descriptions, corrections,
cancel/restart at every step, stale and invalid callbacks, seller rechecking,
failed saves, lost success replies, pagination, photo retries and the compact
shortcut. An additional order-wiring test verifies that old entry buttons leave
an unconfirmed order intact. Existing catalog, photo, store, order and seller-panel
tests also passed.

Automated tests use isolated SQLite data and simulated Telegram HTTP. Manual
Telegram acceptance and live Oracle validation have not been performed for this
change. A manual test should complete entry with and without a photo, correct the
latest input, retry an invalid field, cancel a draft and use the compact shortcut
with a separate test bot. No production products were used as write fixtures.
