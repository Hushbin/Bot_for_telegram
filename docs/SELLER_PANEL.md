# Seller web panel

The frontend lives in the private `Hushbin/smebot-seller` repository and runs on
the former WordPress VM at https://144.24.232.0. The bot remains the backend.
The panel uses the bot's existing tables. The optional Telegram photo cache has an
[additive bot schema migration](PHOTO_DELIVERY.md).

Set `SELLER_PANEL_URL=https://144.24.232.0` in `/etc/smebot/bot.env` to enable it.
`SELLER_API_PORT` defaults to 8081. The API listens on 127.0.0.1 only, within the
Telegram process, and is reached from Nginx through the dedicated reverse SSH
tunnel. Do not bind it to a public interface or copy database credentials to the web VM.

`/panel` and `/start seller_panel` return a five-minute, one-use private sign-in
link to a registered seller. It exchanges a URL fragment for an eight-hour opaque
Secure/HttpOnly/SameSite=Strict cookie. Only token hashes are retained in memory;
restart logs all browsers out. Tokens never appear in server URL access logs.
Sign-in links and browser sessions also carry the account's authentication
generation. Account erasure increments that generation in the same transaction;
re-registration preserves it. Old cookies and unused links cannot become valid
again after re-registration. A stale credential receives HTTP 401 and requires
a fresh `/panel` sign-in. Every request rechecks the live seller account, and mutations require the exact
configured Origin and the expected content type (JSON, or JPEG/PNG for photos). Sellers cannot supply their identity in
request bodies. The frontend stores no products, buyer contacts or sessions in
localStorage. Do not forward sign-in links.

`GET /api/config` returns the bot link; `/api/health` reports API process health.
`POST /api/session` exchanges a token; `POST /api/logout` revokes it.
Authenticated `GET /api/state` returns only the seller's profile/products/orders.
`POST /api/products`, `PUT/DELETE /api/products/{id}`, and `PUT /api/profile` write
the existing tables. `POST /api/orders/{id}` uses `change_order` and its version,
stock, expiry and notification behavior. `ORDERS_ENABLED` controls new purchase
creation only. Existing order actions work with either flag value; `/api/state`
continues to report the actual flag value. Frontends must not disable existing
order controls based on `orders_enabled`. Products with accepted orders cannot be
removed. Removal is soft deletion and retains order history. Product edits supply
`expected_stock` to reject reservations made while an editor was open.

The panel supports the bot's actual order lifecycle. Shipping, draft publication,
private notes and online payments from design concepts are not implemented.
Photos are visible in the product list/editor and can be uploaded or replaced in the panel. Telegram's `/photo` remains available for products without a photo. The bot and panel use USD cents.

## Verification

17 October 2026: new API tests exercise authentication, token replay/expiry,
secure cookies, origin checks, ownership, account erasure, stock conflicts,
product removal, profile updates and order notifications on isolated SQLite data.
The frontend's jsdom tests use an API mock. Production writes are not test fixtures.
See the frontend deployment guide for TLS renewal, tunnel management and rollback.

Validation on 17 October 2026: 247 offline tests passed (43 optional Oracle tests skipped). A separate disposable Oracle schema verified seller create/edit/remove, profile updates, ownership, snapshot serialization, stale-stock rejection, accepted-order removal protection and retained order history. The test runtime was denied production schema access, and both temporary Oracle identities were removed. The embedded API also passed a real loopback HTTP startup/shutdown check.

## Product photos

Authenticated `GET /api/products/{id}/photo` serves only a live seller's own image,
with `Cache-Control: no-store`. Object keys, credentials and public bucket URLs
are never sent to the browser. `PUT` on the same path accepts a raw JPEG/PNG body
up to 5 MiB and requires the exact Origin plus the product's `photo_revision` in
`If-Match`. Decode validation rejects malformed files and images over 16 million
pixels. Images are oriented, resized to fit 2048×2048, and saved as JPEG with a
white background for transparency; EXIF/GPS metadata is discarded.

The upload uses the bot's existing media adapter and photo columns. Attaching the
photo locks the account/product, clears any cached Telegram reference, and rejects a concurrent
photo change. Failed attachment removes the new object; successful replacement
removes the previous object/version. Cleanup failures are logged for operator
follow-up. A pending Telegram upload cannot overwrite a panel photo.

Nginx allows 5 MiB bodies only for the photo route and permits `blob:` image
previews in CSP. Keep its normal JSON request limit at 16 KiB. New products are
saved before their optional photo; if uploading fails, the editor retains the
saved ID and selected photo so retrying cannot create another product.

Photo validation, 17 October 2026: 256 offline tests passed (43 optional Oracle tests skipped). The photo attach/replace SQL, owner checks, stale-upload rejection and Telegram overwrite protection also passed in a disposable Oracle schema; its runtime was denied production access, and the temporary identities were removed.

Chrome UI verification on 17 October 2026 used fictional loopback-only fixture data: existing photo preview, full-size link, native file selection, local preview and saving a replacement PNG all worked. No production product was used as a write fixture.

## Authentication-generation migration and rollout

This release adds `USERS.AUTH_GENERATION`, a non-negative `NUMBER(19)` with a
non-null default of zero. No extra table, privilege or schema quota is required.
The SQLite equivalent is added automatically when the store opens. Profile edits
leave the generation unchanged; erasure increments it only once per live account.
Every protected write checks the browser's generation after acquiring the account
lock, so an earlier snapshot cannot authorize a write into a re-registered account.
Seller snapshots hold that lock through their reads. Photo attachment checks it
inside the upload/attach transaction; a rejected upload removes its new object.
Downloads recheck authorization after reading storage.

1. Rehearse schema setup on a disposable database. Take and verify a current
   recovery export, then stop the bot for the release window.
2. Run `.venv/bin/python scripts/setup_db.py` using the intended database's
   existing secure setup configuration. Existing users receive generation zero;
   repeated setup must preserve subsequent generation values and business data.
3. Deploy this release through the normal workflow and restart the bot. Startup
   refuses a schema missing `AUTH_GENERATION`. Process restart invalidates all
   existing in-memory links/sessions; sellers sign in again through Telegram.
4. Verify API readiness and a fresh seller sign-in. On fictional disposable data,
   verify existing order acceptance/cancellation/completion with new ordering
   paused, and confirm that old cookies and unused links stay rejected after
   erasure and re-registration. Avoid erasing a real account as a test fixture.

Authentication generations are excluded from portable format-1/2 exports.
Restore assigns every imported user a fresh positive generation. Keep the bot
stopped throughout restore and restart with empty session memory before serving
requests. Existing backups need no conversion.

The schema addition is compatible with the previous release, so leave it in place
on rollback. That release lacks these fixes: if reverting, disable the seller
panel (`SELLER_PANEL_URL` empty) until a corrected release is deployed. Reapplying
this migration alone must never reset authentication generations.

The separate frontend repository was unavailable to this implementation session
(HTTP 403). Its `orders_enabled` control gating and HTTP-401 sign-in behavior still
need source/UI verification; no frontend change or live rollout is claimed here.

### Fix verification: 17 October 2026

- Offline suite: 346 tests, 295 passed and 51 optional Oracle checks skipped.
- Full suite against disposable local Oracle Free 23.26.3: all 346 passed,
  including account-generation guards and real concurrent write/erasure tests.
  Initial runs exhausted the local listener's reported connection load; the
  successful run used standard Oracle Easy Connect `retry_count=5&retry_delay=1`.
  No application retry behavior was added for this test environment issue.
- Actual Oracle fresh schema, upgrade of a populated pre-generation schema,
  preserved fixture data, default values, repeat migration and startup refusal
  before migration passed. Actual format-1/2 export/restore assigned fresh
  generations and rejected stale credentials while preserving fixture data.
- API tests cover paused-order lifecycle, ownership, repeated actions, generation
  validation on every mutation, unused sign-in links, old browser cookies,
  re-registration, and photo upload/download races with erasure.
- This session did not access production Oracle or send real Telegram messages.
  Local Oracle differs from the deployed Oracle 19c; production migration, browser
  integration and live rollout remain deployment checks. The disposable database
  and generated test credentials were removed after verification.
