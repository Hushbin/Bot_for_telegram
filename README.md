# SMEbot development app

Telegram bot backed by the existing OCI development Autonomous JSON Database and private `smebot-dev-media` Object Storage bucket. This is a clean start: it does not import FaunaDB records or Cloudinary assets. Legacy source data remains outside this migration scope; this does not assert that the old services are empty.

## Documentation

See [docs/README.md](docs/README.md) for ordering, validation results and remaining improvements. Setup and deployment instructions are below.

Oracle connection-opening latency is logged without credentials or SQL. See
[connection measurements](docs/DATABASE_CONNECTION_METRICS.md) for the journal
summary command and interpretation; connections remain per operation.

## Data model

| Table | Purpose | Key data |
| --- | --- | --- |
| `SME_SCHEMA.USERS` | One profile per Telegram user | Telegram user ID, name, email, phone and whether Telegram verified it, role, preferred category, erasure time |
| `SME_SCHEMA.SELLERS` | Listing profile for a registered seller | Listing name, seller contact email and phone, location, erasure time |
| `SME_SCHEMA.PRODUCTS` | Seller catalog and stock | Global catalog ID, category key, price in minor units (cents) and currency, quantity, optional private photo object key and its version ID, erasure time |
| `SME_SCHEMA.ORDERS` | Confirmed buyer requests | Participants, immutable price/contact snapshots, consent, status/version and expiry |
| `SME_SCHEMA.ORDER_NOTIFICATIONS` | Durable seller and buyer alerts | Event version, recipient, retry schedule, delivery lease and outcome |

Deployed releases enable ordering through [deploy/runtime.env](deploy/runtime.env) (`ORDERS_ENABLED=true`); local runs default to disabled. See [ordering guide](docs/ORDERING.md) for buyer/seller flows, migration and rollout. Pending orders reserve no stock; seller acceptance deducts stock atomically. Payment and fulfillment are arranged directly. Product photos stay in Object Storage; the database stores only their private object keys. Customer preference is one column because the current UI chooses one category. A Telegram user ID, rather than a chat ID or a temporary Fauna record ID, identifies a profile across restarts.

The database's `product_id` identifies a product globally for buyers. Sellers also see a stable number within their own catalog, starting at `#1`; previous listings, including erased ones, keep their place so numbers are never reassigned. This number is derived from existing product rows, so it needs no schema migration.

**Prices.** `products.price_minor` holds the price in minor units (cents) and `products.currency` its ISO code, `USD` by default. The column was called `price_kobo` before this release; `scripts/setup_db.py` renames it. Amounts are shown as `price_minor / 100`, so only currencies with two decimal places are supported; add a per-currency exponent (`catalog.py`) before offering one that has none. No exchange conversion is performed, and amounts entered in another currency before the USD release were never converted.

**Categories.** `products.category` and `users.preferred_category` hold the category *key* (`clothing`, `hardware`, `food`, `art`), never its display label. The keys and labels live in `catalog.py`; rewording a label there changes what users see without touching stored rows. Earlier releases stored the labels (`Clothing/Fashion`, ...), and `scripts/setup_db.py` converts those rows to keys. A category value that matches no old label is left as it is and is displayed unchanged.

`scripts/setup_db.py` creates the `SME_SCHEMA` owner and `SME_BOT` runtime user. The owner has a 20 MB quota and DDL privileges for these tables; the runtime user has only `CREATE SESSION` plus `SELECT`, `INSERT`, and `UPDATE` on the five tables. It has no schema creation, deletion, or ADMIN privileges. The script is rerunnable and skips existing tables and indexes. It also brings an existing database up to date: it adds any column listed in `COLUMN_MIGRATIONS` that a table lacks, renames the columns listed in `COLUMN_RENAMES` (`price_kobo` to `price_minor`), and converts old category labels to keys. Every step checks first, so rerunning it changes nothing. It does **not** rotate existing user passwords, drop columns, or retype columns, and there is no automatic way back: to undo the rename, run `ALTER TABLE products RENAME COLUMN price_minor TO price_kobo` as the schema owner (the category keys would then show as plain text in the old release). The bot checks for the columns it needs at startup and refuses to start with a pointer to this script if they are missing, so **run `scripts/setup_db.py` against the database before deploying a release that adds columns**; otherwise the deploy rolls itself back.

## Personal data

What the bot stores, who sees it, and for how long. New users see the privacy notice after choosing Seller or Buyer; `/privacy` shows it again (`PRIVACY_NOTICE` in `handlers.py`).

| Data | Stored when | Visible to other users |
| --- | --- | --- |
| Telegram ID, account contact name, email, phone (and whether Telegram verified it), role | Registration (`/start`, `/profile`) | Name and phone shared with the selected seller only after explicit order confirmation; email stays private |
| Preferred category | Only when a user chooses one with `/preferences` | No |
| Seller listing name | Seller profile | Yes, shown with each product; may be a shop name, nickname or personal name |
| Seller contact email and phone, location | Seller profile | No |
| Product name, category, description, price, stock, photo | `/addproduct`, `/photo` | Yes |

- **Phone number.** Users either share the number Telegram has verified (a persistent contact-share keyboard; the bot accepts only the user's own contact) or type any number, which is stored as unverified. If the typing keyboard covers the button, tap the keyboard icon beside the message field.
- **Seller listing.** A registered business is not required. Enter a listing name and a city/area, or `Online`, as `name | location`. The account email and phone are copied into seller contact fields unless the seller adds `| email | phone`. The listing name and products are public; the location and contact details are visible only to the operator. Sellers who use their personal name as the listing name make that name public. Existing `name, email, address, phone` input is still accepted.
- **Orders.** Confirmation saves the displayed name, phone, price and consent version/time. Erasure cancels open orders and scrubs that party’s snapshots; redacted transaction history remains for inventory consistency. Already delivered Telegram messages cannot reliably be recalled.
- **Retention.** Data is kept until the user erases it with `/deleteme`; there is no automatic expiry. Users can see what is stored with `/mydata`.
- **Erasure.** `/deleteme` asks for confirmation, then overwrites the user's name, email and phone, their seller profile and their products with `[deleted]`, hides the products, and deletes the photo objects. Rows are overwritten rather than deleted so the runtime database account needs no `DELETE` privilege. The numeric Telegram ID and the erasure time remain. A user who registers again starts from an empty profile.
- **Photos.** Deleting a photo needs its version ID because the bucket is versioned; the ID is stored with the product. A photo stored without one (none are created by this version) only gets a delete marker and needs manual cleanup. If a photo cannot be deleted, the data is still erased, the user is told, and the object key is logged at ERROR.
- **Corrections.** Editing the latest message while entering account details or a compact product line corrects that input; the bot updates the saved record rather than adding a duplicate. Guided product fields can be corrected before Save; a category selection or subsequent field makes earlier messages ineligible. Editing the description refreshes the review and expires its old Save button. After guided Save, use the seller panel for changes. Older edits and edits after a new entry command are ignored.

## Local setup

### Run without OCI

The local backend uses SQLite for profiles, sellers, products, orders and notifications, and files for
photos. It creates `.local/bot.sqlite3` and `.local/media/` automatically; both
are ignored by Git. No OCI account, wallet, database, or bucket is needed.
You still need a Telegram bot token and internet access to Telegram because the
bot receives messages by polling. Use a separate test bot token if your deployed
bot is already polling; only one poller should use a token at a time.

```sh
cd Bot_for_telegram
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.local.example .env
chmod 600 .env
# Replace BOT_TOKEN in .env with your test bot's token.
.venv/bin/python main.py
```

If `.env` already exists with OCI credentials, keep it and change only
`APP_BACKEND=local`; the OCI settings are ignored in this mode. From Telegram,
send `/start`, then try `/sellerprofile`, `/addproduct`, `/products all`, and
`/product <ID>`. Restarting the bot keeps the local data. To start over, stop
the bot and delete `.local/`. The local backend is for functional development;
its SQLite file and photos do not sync to OCI.

### Run with OCI

Use Python 3.10 or newer. Use the existing ignored local configuration when available; a fresh checkout needs database setup and a wallet. Add your Telegram token to the ignored `.env` file before starting the bot. For a fresh checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Fill in `.env` with `BOT_TOKEN`, the `DB_*` connection values, the development database OCID and ADMIN Vault secret OCID, and the bucket namespace/name. Set `DB_PASSWORD` to a unique strong **16–30 byte** Oracle password. Set `DB_WALLET_PASSWORD` to a separate password of at least 16 characters. `DB_DSN=smebotdev_low` is the low service alias in this database wallet. Set `DB_WALLET_DIR` to an absolute path inside the ignored `oci_wallet/` directory. The OCI `DEFAULT` profile must have permission to generate the wallet, retrieve the ADMIN secret, and access objects in the media bucket.

Create an ignored `.setup.env` (mode `0600`) with a different `DB_SCHEMA_PASSWORD` of 16–30 bytes. The runtime bot does not load this file. The setup script retrieves the ADMIN password from OCI Vault using `OCI_ADMIN_SECRET_OCID`; do not copy it into Terraform state or source files. Then run:

```sh
.venv/bin/python scripts/download_wallet.py
.venv/bin/python scripts/setup_db.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python main.py
```

The wallet downloader stores only `tnsnames.ora` and `ewallet.pem` with private permissions. The database uses mTLS; [`python-oracledb` Thin mode](https://python-oracledb.readthedocs.io/en/stable/user_guide/connection_handling.html#mutual-tls-mtls-connection-to-oracle-autonomous-database) requires both files and the wallet password. Keep the ignored `.env`, `.setup.env`, and wallet private. On the OCI Compute VM, set `OCI_AUTH=instance_principal` for Object Storage after creating a matching dynamic group and narrowly scoped IAM policy. OCI Functions use `OCI_AUTH=resource_principal`.

## Bot commands

All commands run in a private Telegram chat.
Telegram's command menu lists every command. `/menu` opens a button menu for the main actions, including guided prompts for product IDs and stock changes.

| Command | Action |
| --- | --- |
| `/menu` | Open the action buttons at any time |
| `/start` | Choose Seller or Buyer, read the privacy notice, then enter account contact details; sellers also choose a listing name and location |
| `/profile` | Update name, email and phone |
| `/privacy` | Show what is stored, who sees it and for how long |
| `/mydata` | Show the data stored about you |
| `/deleteme` | Erase your data, seller profile, products and photos (asks to confirm) |
| `/sellerprofile` | Create or update a seller listing profile |
| `/preferences` | Choose a catalog category |
| `/addproduct` | Enter product fields step by step, review and save, then add an optional photo; compact input also works |
| `/photo [number]` | Attach a photo using your seller product number; prefix a global catalog ID with `G` |
| `/products [all\|clothing\|hardware\|food\|art]` | Browse products, 20 per page, with Older and Newest buttons |
| `/product [catalog ID]` | View details and the private bucket photo if present; accepts `1` or `#1` and asks for an ID when omitted |
| `/order [catalog ID]` | Review a quantity, price and contact sharing before confirming an order |
| `/orders` | View your purchases and cancel pending requests |
| `/sales` | View incoming orders, accept/decline, fulfill or cancel accepted orders |
| `/myproducts` | Browse your listings, 20 per page, with Older and Newest buttons |
| `/manageproduct [number]` | Edit a field, add/replace a photo or confirm removal; prefix a global catalog ID with `G` |
| `/stock [number quantity]` | Set stock using your seller product number; prefix a global catalog ID with `G` |
| `/cancel`, `/skip` | Cancel the current conversation, or skip an optional description or photo with `/skip` |

Registration takes the name and email as `Jane Doe, jane@example.com` (the name may contain commas; the email is what follows the last one). The seller listing format is `name | city/area`, with optional `| email | phone` for separate seller contact details. The earlier `name, email, address, phone` format still works.

`/addproduct` asks for a name, category button, USD price, stock and description
(`/skip` leaves the description empty), then shows a review with Save, Restart
and Cancel buttons. The guided listing is created only on Save. As a shortcut,
send `name | category | price | stock | description` at the first prompt; this
format saves immediately. Cancelling after a save leaves the listing available.
Unfinished drafts are lost on restart. See [guided product entry](docs/GUIDED_PRODUCT_ENTRY.md)
for corrections, validation and save-failure behavior.

A photo must be at most 5 MB. The bot uploads it to a unique private object key and stores the key on the product row. If the database update fails after upload, the bot deletes that exact object version. Photos are never given public Object Storage URLs; `/product` reads the object through the bot's OCI identity and sends it to Telegram.

Use **Manage** in `/myproducts` or your own product view to edit fields after a
review, replace a photo or confirm removal. Pending and accepted orders must be
resolved before removal (elapsed pending requests do not block it). Removal
hides the listing, retains order history and promptly deletes its photo; older
backups may therefore refer to missing media. See
[product management](docs/PRODUCT_MANAGEMENT.md) for concurrency, cleanup and
restart behavior.

For example, if your first listing has catalog ID `#3`, you see `Your product #1 (catalog ID #3)`. Use `/stock 1 5` or `/photo 1` for your own listing; buyers use `/product 3` or `/product #3`. When the bot asks for a catalog ID, replying `3` or `#3` works. `/stock G3 5` and `/photo G3` also accept the global catalog ID explicitly.

Catalogs show newest products first. Tap **Older products** to continue browsing
or **Newest products** to return to the beginning. Navigation keeps the selected
category, works after a bot restart and preserves any unfinished conversation.
New listings appear when returning to Newest; edits and removals affect live
results. See [catalog browsing](docs/CATALOG_PAGINATION.md) for details and validation.

The bot uses polling on both local and VM deployments. It needs a real Telegram token to run; keep it in ignored local configuration or OCI Vault. The bucket is versioned: future photo bytes and retained versions count against the [Always Free Object Storage allowance](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## Dependencies and tests

`requirements.in` lists the direct dependencies as version ranges. `requirements.txt` is the lock generated from it: every package, including transitive ones, pinned with SHA-256 hashes. `pip install -r requirements.txt` therefore verifies hashes automatically, and CI, the deploy build and developers all get the same versions. To change dependencies, edit `requirements.in` and regenerate (needs [`uv`](https://docs.astral.sh/uv/)):

```sh
uv pip compile requirements.in --universal --generate-hashes --python-version 3.10 -o requirements.txt
```

The lock is universal (Python 3.10 and newer, any platform). Install it with `pip`, as shown above. On a very new Python release, a pinned package without a prebuilt wheel is built from source and needs a compiler.

Run the tests with `.venv/bin/python -m unittest discover -s tests -v`. They need no credentials or network. `tests/test_wiring.py` drives the real Telegram handler wiring; `tests/test_store_contract.py` runs one behavioural contract against the in-memory fake and SQLite stores. The same contract runs against Oracle when `ORACLE_TEST_*` variables point at a **throwaway** database (see the file's docstring); those tests are skipped otherwise. The [validation record](docs/ORDERING_VALIDATION.md) documents the completed Oracle checks using isolated disposable schemas, without using live application data as fixtures.

## Deploy to the OCI bot VM

**Before merging a release that adds database columns, run `scripts/setup_db.py` against the database** (see above). The bot refuses to start on an outdated schema, so a deploy made first would roll back, but nothing is damaged either way.

For ordering changes, use the maintenance, backup and staged enablement procedure in the [ordering guide](docs/ORDERING.md#enable-after-migration). Schema changes may not be compatible with the previous release; review rollback compatibility before migrating.

The [deploy workflow](.github/workflows/deploy.yml) tests pull requests, then deploys each successful push to `master` (or a manual run on `master`). It builds wheels on Ubuntu 22.04/Python 3.10, opens a temporary session on the Terraform-managed `smebotdevdeploy` Bastion restricted to that runner's IP, copies a commit archive through an SSH tunnel, and activates a systemd release. CI never uploads `.env` or the database wallet. The [readiness gate](docs/DEPLOYMENT_READINESS.md) checks the existing baseline, candidate process identity, dependencies and backup freshness before reporting activation; rollback distinguishes release failures from external outages. Deployments are serialized. Each job deletes its session and restores the approved administrator `/32` allowlist; the Bastion remains provisioned.

One-time VM setup, from a trusted workstation that can SSH to the VM as `ubuntu`:

1. Generate a separate deployment SSH key: `ssh-keygen -t ed25519 -N '' -f ~/.ssh/smebot-deploy -C smebot-deploy`. Keep its private half off Git.
2. Copy `deploy/bootstrap_vm.sh`, `deploy/activate_release.sh`, `deploy/activation.py`, `deploy/probe_release.sh`, all `deploy/smebot*.service` and `deploy/smebot*.timer` units, and `~/.ssh/smebot-deploy.pub` to one directory on the VM. Run `sudo bash bootstrap_vm.sh /path/to/smebot-deploy.pub` there. This installs Python venv support, creates separate service and deployment users, and installs the health and export timers. For example, from this repository:

   ```sh
   scp deploy/bootstrap_vm.sh deploy/activate_release.sh deploy/activation.py deploy/probe_release.sh deploy/smebot*.service deploy/smebot*.timer ~/.ssh/smebot-deploy.pub ubuntu@<bot-public-ip>:/tmp/
   ssh ubuntu@<bot-public-ip> 'sudo bash /tmp/bootstrap_vm.sh /tmp/smebot-deploy.pub'
   ```
3. Put `tnsnames.ora` and `ewallet.pem` in `/etc/smebot/wallet`, owned by `root:smebot` with mode `0640`. Install [bot.env.example](deploy/bot.env.example) as `/etc/smebot/bot.env`, also owned by `root:smebot` with mode `0640`, after filling in the three Vault secret OCIDs, the development compartment OCID, and bot instance OCID. The file contains no token or passwords in Vault mode.
4. Apply the infrastructure repository's `environments/host/` Terraform stack. Its `smebot-dev-bot-runtime` dynamic group matches **only** the bot VM. Separate policies allow media operations on `smebot-dev-media` and reading only the three named runtime secret bundles. Allow several minutes for a new OCI policy to take effect. The bot uses its [instance principal](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm) for both services.

Existing VMs must follow the [two-stage readiness rollout](docs/DEPLOYMENT_READINESS.md#install-on-an-existing-vm-in-two-stages): deploy and verify instrumentation with the old activator before installing enforcement. Repository changes alone do not update root-owned VM files.

### Versioned runtime settings

Systemd loads `/etc/smebot/bot.env` first, then
`/opt/smebot/current/deploy/runtime.env`. Matching values in the release file win.
Keep release-wide, non-secret settings such as `ORDERS_ENABLED` in
[deploy/runtime.env](deploy/runtime.env). Keep database connections, Vault secret
references, wallet paths, and instance-specific settings in `/etc/smebot/bot.env`;
tokens and passwords continue to come from Vault. Local development still uses
its ignored `.env`.

The deployment archive includes `deploy/runtime.env`. Once the updated units are
installed, each deployment and rollback loads the selected release's settings
when the bot restarts. Configuration changes go through commits and the normal
deployment workflow; deployments do not rewrite `/etc/smebot/bot.env`.

For an existing VM, complete the [ordering migration](docs/ORDERING.md#enable-after-migration)
and deploy a release containing `deploy/runtime.env` before updating systemd.
Then run this one-time update on the VM as an administrator:

```sh
for unit in smebot.service smebot-export.service smebot-health.service; do
  sudo install -m 0644 -o root -g root "/opt/smebot/current/deploy/$unit" "/etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl restart smebot.service
sudo systemctl is-active smebot.service
```

Health and export services read the new settings on their next invocation.
Verify the buyer/seller flow after the restart. A release without
`deploy/runtime.env` cannot start with these units: before rolling back to such
a release, restore its previous service units and run `systemctl daemon-reload`.
Also review the schema and order compatibility restrictions in the ordering guide.

### Move an existing VM to OCI Vault

The development Vault and its software-protected key already exist. Create these three **separate** secrets in that Vault using OCI Console and the values currently used by the VM: `smebot-dev-bot-token` (`BOT_TOKEN`), `smebot-dev-db-runtime-password` (`DB_PASSWORD`), and `smebot-dev-db-wallet-password` (`DB_WALLET_PASSWORD`). Select the existing `smebot-dev-secrets-key`. Do not place secret contents in Terraform, CI variables, shell command arguments, or logs. Record the three secret OCIDs; OCI keeps the secret contents in Vault. The database ADMIN secret is separate and is not readable by the bot VM.

For an existing deployment, switch in stages:

1. Apply the hosting IAM policy and create the secrets. Check the three secret bundles have a current version and wait for IAM propagation.
2. Deploy the Vault-capable bot while the VM's existing `bot.env` still uses `SECRETS_SOURCE=env` (or omits it) and still has plaintext values. Confirm the new release is healthy.
3. Update `/etc/smebot/bot.env` to `SECRETS_SOURCE=oci_vault`, the three `*_SECRET_OCID` entries from [bot.env.example](deploy/bot.env.example), and `OCI_AUTH=instance_principal`. The bot fetches current secret values once at startup and fails closed if any retrieval fails. Restart `smebot.service`; check `systemctl is-active smebot.service`, its journal for errors, `/start`, and one database-backed command.
4. After a healthy Vault-backed restart, remove `BOT_TOKEN`, `DB_PASSWORD`, and `DB_WALLET_PASSWORD` from the VM file and restart once more. Retain a secure recovery copy outside the VM for a manual rollback to an older release, which cannot read Vault. Later secret rotations require a bot restart; coordinate database password rotation with Oracle and token rotation with BotFather.

Local development continues using the ignored `.env` file and `SECRETS_SOURCE=env` (the default). GitHub deployment secrets are not used for these VM runtime credentials. Keep the mTLS wallet files on the VM; Vault stores the wallet **password**, not the wallet files.

### Recovery exports and health checks

Recovery history is written with unique names and create-only requests; only
`latest.json` is replaced after a complete export. The infrastructure recovery
policy restricts overwrite to that exact pointer. The
[infrastructure recovery runbook](https://github.com/Hushbin/oci-infra-for-telegram-bot/blob/main/docs/RECOVERY_AND_HEALTH.md#recovery-object-permissions-and-rollout)
tracks rollout status and the live checks. After applying that policy, run
`scripts/verify_recovery_permissions.py --namespace NAMESPACE` with the deployed
Python environment on the bot VM to probe its instance-principal permissions.
The probe prints exact disposable keys for operator cleanup and does not touch
`latest.json` or real export/erasure records.

The infrastructure repo creates `smebot-dev-recovery`, weekly boot-volume backup policies, and OCI health alarms before this release is deployed. The bot records an erasure marker there before `/deleteme` changes Oracle, so restoring an older daily export cannot reinstate erased profiles. If recovery storage is unavailable, erasure does not start and the user is asked to retry. Current live data is overwritten; exports have a seven-day lifecycle policy, and Oracle-managed backups currently retain copies for up to 60 days. The bot's privacy text explains this.

After deployment, copy and install the four `smebot-export` and `smebot-health` systemd units from `deploy/` as root, run `systemctl daemon-reload`, and enable both timers with `systemctl enable --now smebot-export.timer smebot-health.timer`. Run `systemctl start smebot-export.service` once to establish the first export, then `systemctl start smebot-health.service` and inspect the unit journals and OCI metric streams. The export timer runs daily at 03:00 UTC; the health timer runs every five minutes. The health probe checks systemd, Telegram, Oracle, and export age, then publishes redacted metrics. The recovery runbook in the infrastructure repository covers database import, boot-volume restore, and periodic drills. Restore is an operator-only action against a **new, empty** database: point ignored local `.env` and `.setup.env` at that destination, then run `.venv/bin/python scripts/recovery.py restore exports/<selected>.manifest.json` from the laptop. Never run it against the live DB.

Create a GitHub environment named `dev-bot`, restricted to deployments from `master`. Give it these secrets:

| Secret | Purpose |
| --- | --- |
| `OCI_USER_OCID`, `OCI_TENANCY_OCID`, `OCI_FINGERPRINT`, `OCI_API_PRIVATE_KEY` | API key for the dedicated CI user permitted to update/use only the deployment Bastion and create/read/delete deployment sessions. |
| `BOT_DEPLOY_SSH_PRIVATE_KEY` | Private half of the separate deployment key from step 1. |

Set these environment variables (GitHub **vars**):

| Variable | Value |
| --- | --- |
| `BOT_BASTION_OCID` | `terraform -chdir=environments/host output -raw bot_bastion_ocid` in the infrastructure repo. |
| `BOT_BASTION_IDLE_CIDR` | `terraform -chdir=environments/host output -raw bot_bastion_idle_cidr`; the approved administrator IPv4 `/32`. |
| `BOT_PRIVATE_IP` | `terraform -chdir=environments/host output -raw bot_private_ip`. |
| `BOT_SSH_HOST_KEY` | The VM's trusted SSH host public key, for example `ssh-ed25519 AAAA...`. Obtain it over your existing trusted SSH connection with `ssh ubuntu@<bot-public-ip> 'cat /etc/ssh/ssh_host_ed25519_key.pub'`. |

The infrastructure repository manages the CI IAM policy and persistent Bastion. The deployment identity needs network read access and the tested Bastion/session permissions; network-management and Bastion create/delete grants are unnecessary. The Bastion subnet permits outbound TCP 22 only to the bot VM. The workflow changes the Bastion allowlist to the current runner's `/32` before creating a one-hour session. Its update permission also permits changing the Bastion's session lifetime; the script requires 3,600 seconds and Terraform owns that setting. OCI [documents Bastion as free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), though service limits still apply.

If a runner is killed before cleanup, the next serialized deployment removes leftover `deploy<run-id>a<attempt>` sessions. Unknown session names block deployment for operator investigation. Cleanup failures fail the job and require checking session deletion and the idle allowlist; they do not revert a healthy release. Sessions expire after one hour. Changing the allowlist does not revoke existing sessions, so operators must explicitly delete a stranded session or wait for expiry. Keep this Bastion dedicated to the deployment workflow.

## Seller web panel

Sellers can manage their catalog and orders in the [web panel](https://144.24.232.0). Send `/panel` in a private chat for a sign-in link. See [configuration and API details](docs/SELLER_PANEL.md).

New listing, order and photo-upload budgets are documented in
[durable account limits](docs/ABUSE_LIMITS.md), including migration, observation,
recovery compatibility and enforcement rollout.
