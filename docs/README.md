# Bot documentation

Setup, configuration, commands, privacy and VM deployment are documented in the
[repository README](../README.md). Start there when installing or operating the bot.

| Document | Purpose |
| --- | --- |
| [Durable account limits](ABUSE_LIMITS.md) | Listing, order and upload admission, recovery compatibility and staged rollout |
| [Ordering](ORDERING.md) | Buyer/seller flows, feature flag, Oracle migration, staged rollout, privacy and recovery |
| [Ordering validation](ORDERING_VALIDATION.md) | Dated automated checks and Oracle migration evidence; manual UI test limitations |
| [Conversation lifecycle](CONVERSATION_LIFECYCLE.md) | Inactivity deadlines, silent draft cleanup, correction windows and restart policy |
| [Photo delivery](PHOTO_DELIVERY.md) | Persistent Telegram photo references, timeouts, migration and verification |
| [Improvement suggestions](IMPROVEMENT_SUGGESTIONS.md) | Remaining application and deployment improvements checked against current source |
| [Catalog browsing](CATALOG_PAGINATION.md) | Product pagination, navigation behavior and validation |
| [Guided product entry](GUIDED_PRODUCT_ENTRY.md) | Listing wizard, compact shortcut, corrections, save failures and validation |
| [Product management](PRODUCT_MANAGEMENT.md) | Telegram field editing, photo replacement, confirmed removal, order guards and cleanup |
| [Oracle connection measurements](DATABASE_CONNECTION_METRICS.md) | Safe opening-time logs, local summary commands, interpretation and validation |
| [Deployment readiness](DEPLOYMENT_READINESS.md) | Process/dependency checks, outage classification, bounded rollback, staged installation and validation |

Infrastructure resources, backup operations and secret rotation are documented in
[the infrastructure repository](https://github.com/Hushbin/oci-infra-for-telegram-bot/tree/main/docs).

Keep current guides and unresolved work here. Completed implementation plans and
superseded migration drafts are available in Git history rather than maintained
as competing instructions. `README.md` and `AGENTS.md` remain at the repository root.

- [Seller web panel](SELLER_PANEL.md): authentication, API, configuration and deployment.
