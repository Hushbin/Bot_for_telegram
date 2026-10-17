# Seller web panel

The frontend lives in the private `Hushbin/smebot-seller` repository and runs on
the former WordPress VM at https://144.24.232.0. The bot remains the backend.
No new database tables or schema migration are required.

Set `SELLER_PANEL_URL=https://144.24.232.0` in `/etc/smebot/bot.env` to enable it.
`SELLER_API_PORT` defaults to 8081. The API listens on 127.0.0.1 only, within the
Telegram process, and is reached from Nginx through the dedicated reverse SSH
tunnel. Do not bind it to a public interface or copy database credentials to the web VM.

`/panel` and `/start seller_panel` return a five-minute, one-use private sign-in
link to a registered seller. It exchanges a URL fragment for an eight-hour opaque
Secure/HttpOnly/SameSite=Strict cookie. Only token hashes are retained in memory;
restart logs all browsers out. Tokens never appear in server URL access logs.
Every request rechecks the live seller account, and mutations require the exact
configured Origin and JSON content type. Sellers cannot supply their identity in
request bodies. The frontend stores no products, buyer contacts or sessions in
localStorage. Do not forward sign-in links.

`GET /api/config` returns the bot link; `/api/health` reports API process health.
`POST /api/session` exchanges a token; `POST /api/logout` revokes it.
Authenticated `GET /api/state` returns only the seller's profile/products/orders.
`POST /api/products`, `PUT/DELETE /api/products/{id}`, and `PUT /api/profile` write
the existing tables. `POST /api/orders/{id}` uses `change_order` and its version,
stock, expiry and notification behavior. Products with accepted orders cannot be
removed. Removal is soft deletion and retains order history. Product edits supply
`expected_stock` to reject reservations made while an editor was open.

The panel supports the bot's actual order lifecycle. Shipping, draft publication,
private notes and online payments from design concepts are not implemented.
Photos continue through Telegram's `/photo`. The bot and panel use USD cents.

## Verification

17 October 2026: new API tests exercise authentication, token replay/expiry,
secure cookies, origin checks, ownership, account erasure, stock conflicts,
product removal, profile updates and order notifications on isolated SQLite data.
The frontend's jsdom tests use an API mock. Production writes are not test fixtures.
See the frontend deployment guide for TLS renewal, tunnel management and rollback.
