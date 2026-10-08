# Decision: start with empty application data on OCI

Date: 8 October 2026
Status: accepted by the project owner

The bot was never meaningfully used. The owner has no FaunaDB credentials and has chosen a **clean start** for the OCI version. The new database will begin empty. Existing FaunaDB records, if any, and Cloudinary assets, if any, are outside the migration scope; no export or import is planned.

This is a product decision based on the owner's account of usage, not a verified claim that the old services contain no data. No remote data or accounts are deleted by this decision.

## Consequences for the work plan

- Build and test the onboarding flow with synthetic users through an in-memory `UserStore`. A local SQLite store may be used for manual restart testing. Neither path requires FaunaDB credentials.
- Define the new user record shape and stable Telegram user identifier before choosing and provisioning the OCI database. Create only the target schema/resources needed by the working bot.
- Remove the FaunaDB client dependency when the bot is switched to the new store. There is no requirement to preserve a Fauna adapter or migration script.
- Cloudinary is configured in the code but has no active upload call. Add OCI Object Storage only when a media feature has requirements and tests.
- The Fauna export/import phase, legacy rollback instructions, and total timeline in `Updating_plan.md` are historical draft material, not steps to execute.

If the owner later identifies data that must be preserved, revisit this decision before cutover and obtain authorized source access for an inventory and migration plan.
