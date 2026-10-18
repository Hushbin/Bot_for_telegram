# Catalog browsing

`/products` and `/myproducts` show active products newest first, 20 per page,
split into messages of five listings. **Older products** appears when more
matching listings exist; **Newest products** returns to the beginning from an
older page. The category and seller menu entries use the same navigation.

`/products` defaults to the buyer's preferred category; an explicit category
or `all` overrides that default. Buttons keep the category chosen when browsing
started, even if the preference later changes. `/myproducts` always uses the
Telegram user making the request. Seller listing numbers and global catalog IDs
retain their existing meanings and never change because of pagination.

Navigation sends fresh messages. Buttons work after an application restart and
do not interrupt registration, product entry or unconfirmed orders. They work
only in private chats. If remaining listings were removed, the bot explains that
no older products remain and offers Newest.

## Live results and storage

Oracle and SQLite expose `list_products_page`, returning up to 20 rows and a
`has_more` flag. They fetch at most 21 rows, apply category/seller/erasure filters,
and select IDs below the last displayed ID for the next page. Newly inserted
products cannot shift older results; removing the cursor product does not break
navigation. Existing `list_products` callers still receive the newest 20 rows.

Browsing is a live view: edits, category changes and erasure can change later
results. Return to Newest to see new listings. Navigation supports Older and
Newest; numbered pages and a Previous button are not provided.

No schema migration, new dependency or configuration change is required.

## Validation

On **18 October 2026**, `.venv/bin/python -m unittest discover -s tests -v`
ran 364 tests: **312 passed and 52 optional Oracle tests were skipped** because
no throwaway Oracle database was configured. No live Oracle validation was
performed for this change.

The shared store contract covers 0, 1, 20, 21, 40 and 45 matching listings,
filtering, erased rows, stable seller numbers, new inserts between requests and
removal of the cursor row. Real application wiring tests use SQLite and simulated
Telegram HTTP to check navigation, category preservation, seller isolation,
restart behavior, invalid callbacks, private-chat restrictions, failure handling
and preservation of registration, product entry and unconfirmed orders. Existing
photo delivery, ordering and seller-panel tests also passed.

A manual chat check with more than 20 products in a separate test bot has **not**
been performed. For that check, follow Older and Newest from `/products all`, a
category and `/myproducts`. Automated tests did not contact Telegram or use
production records as write fixtures.
