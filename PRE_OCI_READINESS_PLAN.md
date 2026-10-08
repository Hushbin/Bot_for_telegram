# Bot review and pre-OCI readiness plan

Reviewed: 8 October 2026. Scope assumption: make the existing onboarding path reliable without requiring FaunaDB credentials. The owner chose an [empty-data clean start](CLEAN_START_DECISION.md) for OCI. Product, inventory, and stock features suggested by state names are **not** included because they have no implementation or acceptance criteria. Estimates are focused developer-days for one developer and exclude OCI account setup, Terraform, and hosting.

## Current state

The tracked application is three Python files, about 208 lines total. It uses Python 3.8.10, `python-telegram-bot` 13.7, FaunaDB 4.1.1, Cloudinary 1.26.0, and `python-dotenv` 0.19.0 in a local virtual environment. There is no dependency manifest, test suite, CI, README, or example environment file. Python source parses, but startup and Telegram behavior were not run because they need credentials and external services.

The current bot accepts a comma-separated name, email, and phone number, creates a Fauna `User`, and sets `is_smeowner` for the SME branch. The customer branch only displays category buttons. The Cloudinary upload import is unused, so media storage is not currently part of a working flow.

## Findings, in priority order

1. **Conversation stalls.** `handlers.classer` returns `SME_DETAILS` or `CHOOSE_PREF`, but `main.py` registers handlers only for `CHOOSING` and `CLASS_STATE`. The next SME message and customer category click have no handler. Decide whether to complete these branches or end the conversation after a smaller onboarding MVP.
2. **Input handling can fail.** `Filters.all` passes commands, photos, stickers, and other messages to `choose`; `update.message.text` may be `None`, and `/cancel` can be consumed by the state handler before the fallback. Use a text-only, non-command filter and an explicit invalid-input handler. Answer callback queries and handle unexpected callback data.
3. **User creation is not idempotent.** Every successful `/start` creates another Fauna record. The Fauna record ID needed for the SME update is kept only in `context.user_data`. Define a stable Telegram user key, decide whether existing users resume or update, and persist the required state. Use chat ID separately for messaging.
4. **Sensitive input has no defined rules.** The bot stores name, email, and phone without trimming, validation, or a clear need for each field. Decide which fields the MVP actually needs, provide validation and correction prompts, avoid logging personal data, and document retention/deletion behavior before moving records.
5. **Runtime and operations are not reproducible.** The project has no dependency manifest, setup instructions, example config, structured logging, or error handler. `Updater` and external clients are constructed at import time, which makes startup failures and tests harder to control. Python 3.8 and the installed Telegram library are old; upgrade both together before changing the storage backend.
6. **The existing migration draft overstates current behavior.** `Updating_plan.md` assumes an active Cloudinary upload path and selects an OCI database and image-processing approach before the application data contract is settled. Treat its cost, service, and code examples as unverified proposals. Its Fauna export/import phase is superseded by the clean-start decision.

## How to test without FaunaDB credentials

The current code cannot do this cleanly: `handlers.py` constructs a Fauna client at import time and calls it directly. First move user operations behind a small `UserStore` interface (`get_by_telegram_id`, `upsert_user`, `set_sme_owner`) and pass a store into the handlers. The test implementation should be an **in-memory store**. It can verify the onboarding flow, duplicate prevention, validation, cancellation, and storage-error behavior without a Fauna account, Telegram token, or network calls. Use fake Telegram update/context objects and run the tests with `python -m unittest discover -s tests -v` once they are added.

For manual bot testing with a real Telegram account, a local SQLite implementation can provide persistence across process restarts; this requires a Telegram bot token but **no Fauna credentials**. SQLite is a development adapter, not the proposed OCI database. If there is no Telegram token either, the automated offline tests remain usable, but live Telegram delivery cannot be tested yet.

The owner has confirmed a clean start: there is no legacy export, source-to-target mapping, or Fauna integration test to perform. This does not assert that the old service is empty; its contents are simply outside the new application's scope. Use synthetic records for tests.

## Proposed sequence and effort

| Step | Deliverable | Estimate |
| --- | --- | ---: |
| 1. Freeze the onboarding MVP | Acceptance criteria for `/start`, repeat user, SME/customer choice, cancel, restart, and required personal fields; explicitly defer unbuilt catalog/stock flows | 0.5–1 day |
| 2. Rebuild a supported runtime | Supported Python version, maintained `python-telegram-bot` release, pinned dependencies and lock strategy, README, `.env.example`; convert handlers to the library's current API | 1.5–3 days |
| 3. Repair the conversation | Complete or remove orphan states, text-only input, invalid-input retry, cancel behavior, callback acknowledgement, predictable end states | 1–2 days |
| 4. Define user storage behavior | Small `UserStore` contract, lookup/upsert by Telegram user ID, validation and data shape, clear error behavior; in-memory test implementation | 1–2 days |
| 5. Add behavior checks | Offline tests with a fake store and fake Telegram objects for new/repeat user, SME/customer choice, invalid/non-text input, cancel, and storage failure; CI needs no cloud secrets | 1–2 days |
| 6. Add runtime basics | Startup validation, useful logging without personal data, top-level Telegram error handling, simple run instructions | 0.5–1 day |

Base estimate: **5.5–11 developer-days**. Reserve **1–2 additional days** for dependency-upgrade and implementation surprises: **7–13 developer-days** as a rounded planning range. A SQLite adapter for manual testing and restart checks adds about **0.5–1 day** if needed. The undeveloped SME details, preferences, products, and stock workflows are outside this range and need user stories before estimation.

## Ready to start the OCI migration when

- A fresh checkout installs and runs with documented configuration on a supported Python version.
- The chosen MVP paths finish or recover cleanly; orphan buttons/states are removed or implemented.
- Repeated `/start` does not create duplicate user records, and restart behavior is defined.
- Tests cover the current user journey and storage failure path without live Fauna or Telegram credentials.
- The user data contract is known and the empty-data start is reflected in the deployment plan; only services that support real application paths are selected for OCI.

The first OCI application change can then add an OCI-backed `UserStore` implementation and run the same behavior checks against it. Add Object Storage when a media feature is specified and tested.
