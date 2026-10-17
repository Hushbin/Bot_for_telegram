# Telegram photo delivery

Product views reuse Telegram's `PhotoSize.file_id` after a confirmed upload.
The nullable `products.telegram_photo_file_id` and `telegram_photo_bot_id`
columns persist across restarts. References are scoped to the current bot ID;
`file_unique_id` cannot be used to send a photo. See Telegram's
[sending files documentation](https://core.telegram.org/bots/api#sending-files).

On a cache miss the bot downloads the private object, sends it once, and caches
the largest returned photo. Telegram seller uploads seed this cache after the
object has been attached successfully. Panel uploads remain lazy: their first
Telegram view uploads once. Each view generates the current product caption and
order buttons, including changes to stock or price.

Cache writes compare the live product's object key and nullable version ID.
Replacement, product removal and account erasure clear both cache columns in the
same database update. A completed send for an old photo cannot cache its reference
on a replacement. Invalid-reference clears also compare the bot ID and reference,
so they cannot erase a newer cache entry. Cache persistence failures are logged
without reporting a successful delivery as an outage.

Only an explicit invalid/expired reference `BadRequest` permits a fallback:
conditionally clear it, reload the live product, and upload once. Other Telegram
errors never trigger this fallback. Photo sends set read and write timeouts to
30 seconds; other requests retain their existing settings. A timeout or connection
failure never resends a photo automatically, and preserves an existing cache.
The bot attempts this notice once:

> Telegram hasn’t confirmed delivery yet. The photo may still arrive; please wait before requesting it again.

A failed notice is logged without another generic reply. Storage failures retain
the normal unavailable response, since no Telegram photo send has begun.

## Migration and rollback

Run `scripts/setup_db.py` before deploying this version. Fresh Oracle schemas use
`VARCHAR2(1024 CHAR)` and `NUMBER(19)` for the cache columns. Existing schemas get
two nullable columns through repeatable additive migrations. Startup checks reject
an old schema. SQLite creates or migrates the equivalent `TEXT`/`INTEGER` columns
automatically, preserving existing products and starting their caches empty.

Verify a fresh recovery export before migration, then deploy through the existing
bot CI workflow. The cache is rebuildable and excluded from recovery exports;
format-1 and format-2 restores leave it empty. Rollback to the previous release
retains the compatible nullable columns. No export format change, extra service,
TTL or seller-panel change is needed.

## Verification and operations

Run `python -m unittest discover -s tests -v` for the offline suite. The shared
store contract also runs against Oracle when explicitly configured with
`ORACLE_TEST_*` and `ORACLE_TEST_THROWAWAY=yes`; use a disposable schema whose
runtime cannot access production tables.

`tests/test_photo_delivery.py` exercises cache reuse after reopening SQLite and
rebuilding the real application, different bot IDs, invalid references, concurrent
replacement, Telegram upload seeding, fresh captions/buttons, timeout and connection
handling, notice failures, and cache-write failures. HTTP tests use the real PTB
and HTTPX request path with a simulated transport. They inspect the effective
30-second read/write budgets and simulate six-second successful and 31-second
timed-out responses without waiting or contacting Telegram. Seller API tests
cover panel upload, first-view caching, replacement invalidation and JSON privacy.

Timing logs contain update/product IDs, cache hit/miss and lookup time, storage
download time, and Telegram send time/source/outcome. They exclude tokens,
references, object keys and image contents. Inspect them with:

```sh
sudo journalctl -u smebot.service --since '10 minutes ago' --no-pager | grep 'Photo '
```

The owner should view the same existing photo twice in Telegram. The first cold
view should log `cache miss`, a storage download and `source=upload`; the next
should log `cache hit` and `source=cache` with no storage download. A Telegram
upload that already seeded the cache can hit on its first view. A later view
after an application restart should still hit the cache. Only the owner performs
the live chat acceptance check; production products are never automated write
fixtures.

Validation on 17 October 2026: the offline suite passed 283 tests, with 48 optional
Oracle tests skipped. A disposable Oracle schema passed fresh-schema creation,
migration of a populated old schema, repeated migration, startup rejection before
migration, and format-1/2 export/restore with empty restored caches. Its runtime
was explicitly denied access to production tables. The full Oracle-enabled run
passed all 329 tests, including the shared store and order/race contracts; both
temporary identities and their schema were removed. The latest offline run also
includes two subsequently added panel/caption-button tests (283 passing, 48
Oracle-only skipped). Live owner chat acceptance remains a separate check from
these automated simulations.

Production rollout on 17 October 2026 verified the 16:30:38 UTC recovery export's
SHA-256, byte count, format and table counts before adding the two nullable
columns. Business-data fingerprints were unchanged, existing caches were empty,
and a second migration was a no-op. The runtime account could read the columns.
[CI run 39050268632](https://github.com/Hushbin/Bot_for_telegram/actions/runs/39050268632)
successfully deployed application release `aa07025`. Bot service, Telegram,
database, fresh-backup and public seller API health checks passed. Automated tests
did not send Telegram messages or use production products as write fixtures.

The owner completed the live first/repeat-view check on 17 October 2026 and
reported both views were fast, with the second a little faster. Production timing
logs confirmed a cache miss, a 52.4 ms storage download and a confirmed 217.9 ms
Telegram upload for product 21. Its repeat view was a cache hit with a confirmed
71.9 ms Telegram send and no storage download. Database lookup times were 75.7 ms
and 77.4 ms respectively. Restart persistence was verified by automated database
reopen/application rebuild tests; the live check did not restart the bot.
