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
Every request rechecks the live seller account, and mutations require the exact
configured Origin and the expected content type (JSON, or JPEG/PNG for photos). Sellers cannot supply their identity in
request bodies. The frontend stores no products, buyer contacts or sessions in
localStorage. Do not forward sign-in links.

`GET /api/config` returns the bot link; `/api/health` reports API process health.
`POST /api/session` exchanges a token; `POST /api/logout` revokes it.
Authenticated `GET /api/state` returns only the seller's profile/products/orders.
`POST /api/products`, `PUT/DELETE /api/products/{id}`, and `PUT /api/profile` write
the existing tables. `POST /api/orders/{id}` uses `change_order` and its version,
stock, expiry and notification behavior. Products with accepted or unexpired
pending orders cannot be removed (HTTP 409). Elapsed pending requests do not
block deletion. Removal is soft deletion, retains order history and promptly
deletes the stored photo version. Product edits supply
`expected_stock` to reject reservations made while an editor was open.

The panel supports the bot's actual order lifecycle. Shipping, draft publication,
private notes and online payments from design concepts are not implemented.
Photos are visible in the product list/editor and can be uploaded or replaced in the panel. Telegram's `/photo` remains available for products without a photo. The bot and panel use USD cents.
Telegram also offers `/manageproduct` and Manage buttons for field editing,
replacement and removal. Both interfaces use the shared
[product-management safeguards and cleanup service](PRODUCT_MANAGEMENT.md).

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
