# SMEbot improvement suggestions

Reviewed: 13 October 2026, against the working tree at commit `0bb7694` (plus the uncommitted deletion of `CLEAN_START_DECISION.md`). Scope: the application logic in `main.py`, `handlers.py`, `database.py`, `local_backend.py`, `storage.py`, `config.py`, the tests, and the parts of CI/deploy that affect runtime behaviour.

## How to read this document

Every finding carries an evidence tag:

- **[reproduced]** I drove the real wired application (`main.build_application`, python-telegram-bot 22.8) with synthetic Telegram updates and an offline transport, and observed the behaviour. Reproduction steps are given in plain Telegram actions so you can repeat them by hand.
- **[code]** Read from the source, not executed. Treat these as well-founded observations, not measured facts.

Effort is a rough size, not a commitment: **S** under half a day, **M** half a day to a day, **L** one to three days.

## Priorities at a glance

| Section | Item | Tag | Effort |
| --- | --- | --- | --- |
| 1.1 | Bot token is written to the logs on every Telegram call | reproduced | S |
| 1.2 | Two conversations share a user but not state; `/profile` + `/addproduct` breaks product entry | reproduced | M |
| 1.3 | Editing a message crashes the handler with no reply | reproduced | S |
| 1.4 | Unmatched input and stale buttons get silence | reproduced | S |
| 1.5 | No global error handler | code | S |
| 2.1 | Tests that exercise the wiring, not just handler methods | reproduced | M |
| 3.1, 3.2 | Decide what personal data is needed; add retention and deletion | code | M each |
| 4.1, 4.2 | Fix the data model while the tables are still empty | code | S each |
| 5.1–5.5 | Make the catalog usable: contact seller, edit/delete, wizard input, pagination | code | S–L |
| 6.1 | Survive restarts: timeouts, persistence | code | M |
| 6.2, 6.3 | Efficiency: connection pool, cached Telegram `file_id` | code | M each |

Section 1 and item 2.1 are defects and their safety net; they are worth doing before adding features.

## Implementation status

Updated 13 October 2026. **Done** (uncommitted in the working tree):

| Section | What changed |
| --- | --- |
| 1.1 | `main.configure_logging()` raises the `httpx` logger to WARNING. If the old build ran on the VM, consider rotating the bot token (see 1.1). |
| 1.2 | `main.py` now builds one `ConversationHandler` with every entry command and state. One `/cancel` clears everything, and any entry command replaces the current state. |
| 1.3 | All message handlers use `filters.UpdateType.MESSAGE`, so edits are ignored. Handlers read `update.effective_message`. |
| 1.4 | Each state ends with catch-alls that say what the bot is waiting for. Buttons no conversation owns get an "expired, send /start" alert. Commands are excluded from the catch-alls, so `/products` etc. still work mid-flow. |
| 1.5 | `handlers.on_error` is registered; `run_polling` is limited to `message` and `callback_query`. |
| 2.1, 2.2 | New `tests/test_wiring.py` (20 tests) drives the real wired application offline. It covers seller and customer journeys, repeat `/start`, cancel, isolation between flows, non-text and unexpected input, edits, stale buttons, group chats, storage failure and the global error handler. Against the pre-change code, 12 of the 20 fail. |
| 6.4 | The `private_only` boilerplate is replaced by `filters.ChatType.PRIVATE` on every handler. Bot commands sent in a group get one "message me privately" reply, and commands addressed to other bots are ignored. |
| 7 | Stale `main.py` docstring fixed. |
| extra | `SQLiteStore.connect()` now closes its connections (it only committed before), which removed `ResourceWarning: unclosed database` noise from the tests. |
| 3.1 | Personal data is still collected, as decided. New users see a privacy notice before giving anything (`/privacy` repeats it), and sellers are told what other users can see. The phone step offers Telegram's verified contact button or a typed number; only the user's own contact counts as verified, and the flag is stored. `/mydata` shows what is stored. |
| 3.2 | The README documents retention (kept until erased, no automatic expiry). `/deleteme` asks for confirmation, then overwrites personal data with `[deleted]`, hides products and deletes photos, using UPDATEs only, so the runtime account still has no `DELETE` grant. New columns `phone_verified`, `deleted_at` (users, sellers, products) and `photo_version_id`; `setup_db.py` adds them to an existing database, and the bot refuses to start on an outdated schema. |
| edits | Editing your latest input corrects it (name and email, phone, business details, product line) and updates what was saved rather than duplicating it. Older messages and edits after another command are ignored. |
| 2.3 | `tests/test_store_contract.py` runs one contract (13 tests) against the in-memory fake and SQLite, and against Oracle only when `ORACLE_TEST_*` points at a throwaway database. The shared `FakeStore` moved to `tests/fakes.py`; the unused `set_role` was removed. |
| 2.4 | `requirements.in` holds the ranges; `requirements.txt` is a universal hashed lock (34 packages). Verified by installing with hash checking on Python 3.10 and 3.15 and running the suite, and by confirming that every pin has a Linux cp310 wheel with a matching hash, which the VM's `--no-index` install needs. |
| parsers | The name may contain commas, and the business address may contain commas. |

**Not done yet:** 4.1, 4.2, 5.x, 6.1, 6.2, 6.3, 6.5, and the remaining housekeeping items. 4.x needs your decision on schema renames; 6.1 needs the `python-telegram-bot[job-queue]` extra.

**Not verified here:** nothing was run against the real Oracle database, Object Storage, or Telegram. The Oracle SQL (`MERGE`, the erase transaction, `setup_db.py`'s `ALTER TABLE`) and OCI photo deletion are covered only by the SQLite and fake equivalents plus mocks. The Oracle contract tests exist but are untested. Run `scripts/setup_db.py` and the Oracle contract tests against a non-production database before the first deploy. Also check that the bot's IAM policy allows `OBJECT_DELETE` on the bucket; the README says it does, but that policy lives in the infrastructure repository.

---

## 1. Correctness and safety defects

### 1.1 The bot token is logged on every Telegram call [reproduced] — S

`main.py` calls `logging.basicConfig(level=logging.INFO)`. python-telegram-bot 22 uses `httpx`, and `httpx` logs each request URL at INFO. Telegram puts the token in the URL path, so the log line is:

```
httpx: HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getUpdates "HTTP/1.1 200 OK"
```

I reproduced this with a fake token and a mock transport. With polling, this line is written on every `getUpdates` cycle, and the systemd unit sends stdout to the journal. Setting the `httpx` logger to WARNING removed the line completely.

Fix, in `main()` before `run_polling()`:

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

Because the VM has been running with INFO logging, the token has probably been written to its journal (`journalctl -u smebot`). I did not look at the VM. If anyone other than you can read those logs, or they are shipped anywhere, rotate the token through BotFather after deploying the fix.

### 1.2 Two `ConversationHandler`s track the same user independently [reproduced] — M

`main.py` registers `registration` (states 0–3) and `catalog` (states 4–5) as separate conversations. A command that starts one conversation does not reset the other, and the one registered first in the handler group answers plain text first. Observed:

| Steps (registered seller) | Result |
| --- | --- |
| `/profile`, then `/addproduct`, then send a valid product line | Bot replies *"Enter name, email, phone separated by commas."* No product is created. |
| `/addproduct`, abandon it, later `/start`, then type `hello` | Bot replies *"Use: name \| category \| price \| stock \| description"* to an unrelated message. |
| `/profile`, `/addproduct`, then one `/cancel`, then a product line | `/cancel` ends only the registration conversation; the product line is then accepted and **a product is created** after the user cancelled. |

Cause: a state handler in the first conversation wins over an entry point of the second, and neither conversation knows about the other.

Fix: use one `ConversationHandler` with all entry commands (`start`, `profile`, `sellerprofile`, `preferences`, `addproduct`, `photo`) and all states. With `allow_reentry=True`, any entry command then replaces whatever state the user was in, and a single `/cancel` clears everything. Add `filters.ChatType.PRIVATE` to the entry points here (see 6.4).

### 1.3 Edited messages crash handlers [reproduced] — S

`filters.TEXT & ~filters.COMMAND` also matches `edited_message` updates (and channel posts and business messages; see the filter's own docstring in the library). For those, `update.message` is `None`, so:

- Sending a corrected version of your contact details while the bot waits for them raises `AttributeError: 'NoneType' object has no attribute 'text'`, and the user gets no reply.
- Editing a `/start` message raises `AttributeError: ... 'reply_text'`.

Users fix typos by editing messages, so this is a likely path, not an edge case. Today it only produces a log traceback and silence.

Fix: add `filters.UpdateType.MESSAGE` to the message handlers (`MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, ...)`), and read `update.effective_message` in handlers rather than `update.message`. Optionally treat an edit as a correction instead of ignoring it.

### 1.4 Unmatched input and stale buttons get silence [reproduced] — S

- In `PRODUCT_PHOTO` (waiting for a photo), sending text gets no reply. In `CLASS_STATE` (waiting for a Seller/Customer tap), typing text gets no reply. The user cannot tell whether the bot is alive.
- An inline button tapped when no conversation is active, for example after a restart or a deploy, is never answered, so the Telegram client shows a loading spinner on it.

The earlier readiness plan asked for "an explicit invalid-input handler"; the text filter was fixed, but this part was not.

Fix: add a catch-all to the conversation's `fallbacks`, excluding commands so `/products` and friends still work mid-conversation:

```python
fallbacks=[
    CommandHandler("cancel", handlers.cancel),
    MessageHandler(~filters.COMMAND & filters.UpdateType.MESSAGE, handlers.unexpected),
    CallbackQueryHandler(handlers.unexpected),
]
```

`unexpected` should say what the bot is waiting for and mention `/cancel`, and for callbacks should call `query.answer()` first. Fallbacks run only when no state handler matched, so this does not interfere with valid input. A second, top-level `CallbackQueryHandler` (added after the conversation) that answers with "This menu has expired, send /start" covers buttons from before a restart.

### 1.5 No global error handler [code] — S

No `add_error_handler` is registered, so any exception outside a handler's own `try/except` (for example the two above, or a Telegram API error while replying) is logged by the library and the user gets nothing. The earlier plan listed "top-level Telegram error handling" as a deliverable.

```python
async def on_error(update, context):
    LOG.error("Unhandled error, update_id=%s", getattr(update, "update_id", None),
              exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again.")

application.add_error_handler(on_error)
```

Also pass `allowed_updates=["message", "callback_query"]` to `run_polling()` so the bot stops receiving update types it never handles.

---

## 2. Tests and CI

### 2.1 Test the wiring, not just the handler methods [reproduced] — M

`tests/test_bot.py` calls `BotHandlers` methods directly with fake objects. That cannot see problems in `main.py`: a state that is returned but not registered (the original stall), two conversations interfering (1.2), edited messages (1.3), or silent states (1.4). All four of those reproduced defects live in the wiring.

A small harness is enough. Patch the library's HTTP layer, build the real application, and feed it `Update.de_json(...)` objects:

```python
from telegram.request import HTTPXRequest

async def fake_do_request(self, url, method, request_data=None, *a, **kw):
    api = url.rsplit("/", 1)[-1]
    sent.append((api, dict(request_data.parameters) if request_data else {}))
    result = {"getMe": {"id": 1, "is_bot": True, "first_name": "B", "username": "b"},
              "answerCallbackQuery": True}.get(api) or {
        "message_id": len(sent), "date": 0, "chat": {"id": 1, "type": "private"}}
    return 200, json.dumps({"ok": True, "result": result}).encode()

HTTPXRequest.do_request = fake_do_request      # restore in tearDown
app = build_application(settings, SQLiteStore(tmp / "db"), LocalMediaStorage(tmp / "m"))
await app.initialize()
await app.process_update(Update.de_json({...}, app.bot))
```

Scenarios to cover first: new seller and new customer end to end; `/profile` then `/addproduct`; one `/cancel` clears everything; text in a button-only state gets a reply; edited message does not raise; a stale button gets answered. I can turn the probe I used for this review into `tests/test_wiring.py` if you want it.

### 2.2 Close the gaps the readiness plan listed [code] — S

The plan's step 5 called for tests of cancel, customer choice, non-text input, and storage failure. Currently `tests/test_bot.py` has no test for `/cancel`, no customer path (`role:customer` then a category), no non-text input, and only one storage-failure case (photo update). The failure path for user and product writes (`self.failure(update)`) is untested.

### 2.3 Run one contract suite against every store [code] — M

There are three store implementations: `FakeStore` in the tests, `SQLiteStore`, and `OracleStore`. `OracleStore` contains all the production SQL and has **no tests at all**. Drift has already started: the Oracle schema has `created_at`/`updated_at`, SQLite does not, and `set_role` exists in both stores but is never called by the application (only by the test fake).

Write the store tests once as a mixin (`upsert_user` is idempotent, `set_stock` rejects non-owners, `list_products` filters and orders, and so on) and run it against `FakeStore` and `SQLiteStore` always, and against `OracleStore` in CI when a test database is available (for example the `gvenzl/oracle-free` container as a service). Remove `set_role` if you do not plan to use it.

### 2.4 Make dependencies reproducible [code] — S

`requirements.txt` has ranges only (`python-telegram-bot>=22.6,<23`). The CI `test` job and the `deploy` job each resolve those ranges independently, and deploy builds its wheelhouse from the ranges again, so what is deployed is not guaranteed to be what was tested. The readiness plan asked for "pinned dependencies and lock strategy"; that is still open.

Generate a hashed lock file (`uv pip compile --generate-hashes` or `pip-tools`), install and build the wheelhouse from it in both jobs, and let Dependabot or Renovate propose updates. Also consider a CI matrix of Python 3.10 (what the VM runs) and the newest release (your local `.venv` is 3.15), and adding `ruff` to CI.

---

## 3. Privacy and data minimisation

### 3.1 Decide what personal data is needed [code] — M

Registration collects name, email and phone from every user, and sellers add business email, address and phone. In `handlers.py` only `full_name`, `role` and `preferred_category` are ever read back; email and phone are never used, and the business contact details are never shown to anyone (`product_text` shows only the business name). The earlier plan asked to "decide which fields the MVP actually needs"; no decision is recorded.

Options:

- If nothing uses them, stop collecting them. Registration then reduces to choosing a role, which also removes most of the parsing code.
- If customers should reach sellers (see 5.1), show the seller's business contact details, with the seller's knowledge, and keep collecting those.
- For phone numbers, Telegram's contact-share button (`KeyboardButton(request_contact=True)`) returns a number Telegram has verified, and replaces the regex and typing.

### 3.2 Retention and deletion [code] — M

Nothing in the README states how long data is kept or how a user can have it removed, and the application cannot do it: the runtime database user is granted only `SELECT, INSERT, UPDATE`, and there is no delete command.

Suggested minimum:

- Write down retention in the README: what is stored, why, and for how long.
- Add a `/deleteme` command that erases or anonymises the user's name, email, phone and seller details. This can be done with `UPDATE` only, which keeps the least-privilege design; add an `is_active`/`deleted_at` column rather than granting `DELETE`.
- Remove the user's product photos. The bucket is versioned, so deleting an object without its version ID leaves the versions in place; delete each version.

---

## 4. Data model (cheap to change now, costly later)

The README says the three tables are empty. I could not check the database, but if that is accurate, these changes need no data migration today.

### 4.1 Rename `price_kobo` [code] — S

The column is named after the Nigerian kobo but now stores USD cents; the README carries a paragraph explaining the mismatch. Rename to `price_minor` (or `price_cents`) and add a `currency` column (`CHAR(3)`, default `'USD'`). Update `schema.sql`, both stores, `handlers.py` and the tests together. `setup_db.py` skips existing tables, so an existing database needs an explicit `ALTER TABLE ... RENAME COLUMN`.

### 4.2 Store the category key, not the display label [code] — S

`products.category` and `users.preferred_category` hold the display text (`"Clothing/Fashion"`), taken from `CATEGORIES`. Renaming a label in code then silently orphans every existing row. Store the key (`clothing`) and map to the label only when displaying.

---

## 5. Product gaps that limit the catalog

### 5.1 Customers cannot contact a seller [code] — M

The product view shows business name, category, price and stock, and nothing else. A customer has no way to ask a question or buy. The seller's business email, address and phone are collected and stored but never displayed. Even before orders exist, a "Contact seller" line (the business phone/email the seller entered, or a Telegram link if the seller has a username) turns the catalog from a list into something usable. The README is explicit that orders and payments are out of scope, so this is the smallest step that makes browsing meaningful.

### 5.2 Products cannot be corrected or removed [code] — L

After `/addproduct`, the only changes possible are stock (`/stock`) and adding a first photo (`/photo` refuses when one exists). A typo in the name or price is permanent, and there is no way to delete a product or replace a photo. Add `/editproduct` and `/deleteproduct` (soft delete, see 3.2) and allow replacing a photo.

### 5.3 Replace the pipe-delimited and comma-delimited input [code] — L

`name, email, phone` and `name | category | price | stock | description` are easy to get wrong: a name such as "Doe, Jane" fails `parse_contact`, a product name containing `|` breaks `parse_product`, and the user must remember the category keys. A step-by-step flow (name, then category via the inline keyboard you already use for preferences, then price, then stock, then description) validates one thing per message and gives better errors. If you do 1.2 first, this fits naturally as more states in the one conversation.

### 5.4 Pagination [code] — M

`/products` and `/myproducts` stop at a hard-coded 20 rows. Add a "More" inline button using keyset paging on `product_id` (`WHERE product_id < :last ORDER BY product_id DESC`); the existing indexes on `(category, product_id)` and `(seller, product_id)` already support that. Consider hiding or marking items with stock 0 in the customer view.

### 5.5 Discoverability and small UX points [code] — S

- Call `bot.set_my_commands([...])` in a `post_init` hook so Telegram shows a command menu; today only the README lists commands.
- `/profile` for an existing customer asks "seller or customer?" again each time (`choose` only skips that question for sellers). Show the current values and skip the role question for anyone already registered.
- After a button tap, remove the buttons (`query.edit_message_reply_markup(None)`) so a double tap does not start the next step twice. `classer` and `preference` use `query.message.reply_text`; for old messages Telegram may deliver an inaccessible message object, so `context.bot.send_message(chat_id=...)` is more robust.

---

## 6. Resilience and efficiency

### 6.1 In-flight conversations are lost on every restart [code] — M

The application has no persistence and no conversation timeout, and every merge to `master` restarts the service. A user in the middle of registration or `/addproduct` is silently dropped on restart. In the other direction, a flow the user abandons never expires while the process keeps running, which is how the stale-state case in 1.2 arises.

- `conversation_timeout=900` on the conversation expires abandoned flows. It relies on the library's job queue, which needs the `python-telegram-bot[job-queue]` extra; APScheduler is not installed in the current `.venv` or listed in `requirements.txt`.
- `PicklePersistence` keeps state across restarts. The systemd unit uses `ProtectSystem=strict`, so you would also need `StateDirectory=smebot` and a path under it.
- With 1.4's expired-menu handler, in-flight users at least get a clear message rather than silence.

### 6.2 Database connections and throughput [code] — M

`OracleStore.connect()` opens a new mutual-TLS connection to Autonomous Database for every store call; a single `upsert_user` is two connections. Use `oracledb.create_pool(min=1, max=4, ...)` and acquire from it. Separately, `concurrent_updates(False)` processes one update at a time, so a slow database call delays every other user. That is fine at small scale; measure before changing it, since concurrent updates for the same user need care around conversation state.

### 6.3 Re-uploading product photos on every view [code] — M

`/product` downloads the photo from Object Storage and uploads the bytes to Telegram each time. After the first send, Telegram returns a `file_id` that can be reused for free. Store it (`telegram_file_id` on the product), send by `file_id`, and fall back to Object Storage if Telegram rejects it. Object Storage stays the source of truth; you save a bucket read, memory and latency per view, which also matters for the Always Free allowance mentioned in the README.

### 6.4 Remove per-handler boilerplate [code] — S

Ten handlers start with `if await self.private_only(update): ...`. A `filters.ChatType.PRIVATE` filter on the handlers does this once, and the group-chat reply can be one small handler.

### 6.5 Abuse limits [code] — S

Anyone who registers can become a seller with no approval (the README states there is none), with no cap on products, photos, or message rate. Storage is quota-limited (20 MB database quota, Always Free bucket allowance), so one account could fill it. A per-seller product cap, and a simple per-user cooldown on writes, are cheap safeguards.

---

## 7. Housekeeping

- **Dangling link (resolved).** `CLEAN_START_DECISION.md` was deleted in the working tree when this review began, while `Updating_plan.md` and `PRE_OCI_READINESS_PLAN.md` link to it. It is present again, so the links work.
- **Stale docstring.** `main.py` says "Polling is for local development", but the README and the systemd unit use polling in production.
- **Startup warning.** The library prints a `PTBUserWarning` about `per_message=False` and `CallbackQueryHandler` each time the bot starts. It is informational for this design; acknowledge it with `warnings.filterwarnings` so real warnings are not hidden by it.
- **Old virtualenv.** `bin/`, `lib/` and `pyvenv.cfg` at the repository root are a leftover Python 3.8 environment from the Fauna era (ignored by Git). The working environment is `.venv/`. They are safe to delete locally; I did not touch them.
- **Deploy health check.** `activate_release.sh` waits 10 seconds and checks `systemctl is-active`. A bot that is running but cannot reach Telegram or the database counts as healthy. Logging a "ready" line after `store.check()` and polling for it, or using `Type=notify`, would be a stronger gate. This is outside the application logic, so I only note it.

## Suggested order

1. **Same day:** 1.1 (log leak), 1.3, 1.4, 1.5. Small, independent, and each removes a user-visible failure or a risk.
2. **Next:** 1.2 (single conversation) together with 2.1 (wiring tests). Doing them together means the fix is proven by a test and cannot regress.
3. **Before real users:** 3.1 and 3.2 (data minimisation, retention, deletion), and 4.1/4.2 while the tables are empty.
4. **Then product work:** 5.1, 5.2, 5.3, 5.4.
5. **Operational hardening:** 6.1, 6.2, 6.3, 2.3, 2.4.

## What I did not verify

- Nothing was run against real Telegram, the OCI database, Object Storage, or the VM. All `[reproduced]` results come from an offline harness using the real handler wiring and the SQLite backend, so Oracle-specific SQL behaviour is untested.
- I did not open `.env`, `.env.vm`, `.setup.env`, or `oci_wallet/`, and I did not inspect the VM's logs or the contents of the deployed database. Statements about log exposure and empty tables rest on the code and the README.
- Performance observations (6.2, 6.3) come from reading the code, not from measurement.
