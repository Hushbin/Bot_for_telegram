# SMEbot development app

Telegram bot backed by the existing OCI development Autonomous JSON Database and private `smebot-dev-media` Object Storage bucket. This is a clean start: it does not import FaunaDB records or Cloudinary assets. The old `Updating_plan.md` remains historical context.

## Data model

| Table | Purpose | Key data |
| --- | --- | --- |
| `SME_SCHEMA.USERS` | One profile per Telegram user | Telegram user ID, name, email, phone and whether Telegram verified it, role, preferred category, erasure time |
| `SME_SCHEMA.SELLERS` | Business profile for a registered user | Business name, email, address, phone, erasure time |
| `SME_SCHEMA.PRODUCTS` | Seller catalog and stock | Category, USD price in cents, quantity, optional private photo object key and its version ID, erasure time |

The bot does not have orders, payments, reservations, or seller approval, so it does not create tables for those features. Product photos stay in Object Storage; the database stores only their private object keys. Customer preference is one column because the current UI chooses one category. A Telegram user ID, rather than a chat ID or a temporary Fauna record ID, identifies a profile across restarts.

For compatibility with existing databases, the price column is still named `price_kobo`; it now stores USD cents. Reprice any products entered in NGN before displaying them with the USD version of the bot. No exchange conversion is performed.

`scripts/setup_db.py` creates the `SME_SCHEMA` owner and `SME_BOT` runtime user. The owner has a 20 MB quota and DDL privileges for these tables; the runtime user has only `CREATE SESSION` plus `SELECT`, `INSERT`, and `UPDATE` on the three tables. It has no schema creation, deletion, or ADMIN privileges. The script is rerunnable and skips existing tables and indexes. It also adds any column listed in `COLUMN_MIGRATIONS` that an existing table lacks (the privacy and erasure columns of this release), and nothing else: it does **not** rotate existing user passwords, and it cannot drop, rename or retype columns. The bot checks for those columns at startup and refuses to start with a pointer to this script if they are missing, so **run `scripts/setup_db.py` against the database before deploying a release that adds columns**; otherwise the deploy rolls itself back.

## Personal data

What the bot stores, who sees it, and for how long. The same text is shown to users at `/start` and by `/privacy`; keep the two in step (`PRIVACY_NOTICE` in `handlers.py`).

| Data | Stored when | Visible to other users |
| --- | --- | --- |
| Telegram ID, name, email, phone (and whether Telegram verified it), role, preferred category | Registration (`/start`, `/profile`, `/preferences`) | No |
| Business name | Seller profile | Yes, shown with each product |
| Business email, address, phone | Seller profile | No |
| Product name, category, description, price, stock, photo | `/addproduct`, `/photo` | Yes |

- **Phone number.** Users either share the number Telegram has verified (a contact-share button; the bot accepts only the user's own contact) or type any number, which is stored as unverified.
- **Retention.** Data is kept until the user erases it with `/deleteme`; there is no automatic expiry. Users can see what is stored with `/mydata`.
- **Erasure.** `/deleteme` asks for confirmation, then overwrites the user's name, email and phone, their seller profile and their products with `[deleted]`, hides the products, and deletes the photo objects. Rows are overwritten rather than deleted so the runtime database account needs no `DELETE` privilege. The numeric Telegram ID and the erasure time remain. A user who registers again starts from an empty profile.
- **Photos.** Deleting a photo needs its version ID because the bucket is versioned; the ID is stored with the product. A photo stored without one (none are created by this version) only gets a delete marker and needs manual cleanup. If a photo cannot be deleted, the data is still erased, the user is told, and the object key is logged at ERROR.
- **Corrections.** Editing the latest message while entering details (name and email, phone, business details, a product line) corrects that input; the bot updates what it saved rather than adding a duplicate. Edits of older messages, or after another command, are ignored.

## Local setup

### Run without OCI

The local backend uses SQLite for profiles, sellers, and products, and files for
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

Use Python 3.10 or newer. This checkout already has the three empty tables, a protected local wallet, and ignored local database credentials. Add your Telegram token to the ignored `.env` file before starting the bot. For a fresh checkout:

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

| Command | Action |
| --- | --- |
| `/start` | Register once (privacy notice, name and email, then phone) or see the menu on return |
| `/profile` | Update name, email and phone |
| `/privacy` | Show what is stored, who sees it and for how long |
| `/mydata` | Show the data stored about you |
| `/deleteme` | Erase your data, seller profile, products and photos (asks to confirm) |
| `/sellerprofile` | Create or update a business profile |
| `/preferences` | Choose a catalog category |
| `/addproduct` | Add name, category, USD price, stock, description, then one optional photo |
| `/photo <ID>` | Attach a photo to one of your products that has none |
| `/products [all\|clothing\|hardware\|food\|art]` | Browse the latest 20 products |
| `/product <ID>` | View details and the private bucket photo if present |
| `/myproducts` | List your latest 20 products |
| `/stock <ID> <quantity>` | Set stock on one of your products |
| `/cancel`, `/skip` | Stop the current conversation, or finish a product without a photo |

Registration takes the name and email as `Jane Doe, jane@example.com` (the name may contain commas; the email is what follows the last one). Business details are `name, email, address, phone`; the address may contain commas. The product input format is `name | category | price | stock | description`. A photo must be at most 5 MB. The bot uploads it to a unique private object key and stores the key on the product row. If the database update fails after upload, the bot deletes that exact object version. Photos are never given public Object Storage URLs; `/product` reads the object through the bot's OCI identity and sends it to Telegram.

The bot uses polling on both local and VM deployments. It needs a real Telegram token to run; no token is present in this checkout. The bucket is versioned: future photo bytes and retained versions count against the [Always Free Object Storage allowance](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## Dependencies and tests

`requirements.in` lists the direct dependencies as version ranges. `requirements.txt` is the lock generated from it: every package, including transitive ones, pinned with SHA-256 hashes. `pip install -r requirements.txt` therefore verifies hashes automatically, and CI, the deploy build and developers all get the same versions. To change dependencies, edit `requirements.in` and regenerate (needs [`uv`](https://docs.astral.sh/uv/)):

```sh
uv pip compile requirements.in --universal --generate-hashes --python-version 3.10 -o requirements.txt
```

The lock is universal (Python 3.10 and newer, any platform). Install it with `pip`, as shown above. On a very new Python release, a pinned package without a prebuilt wheel is built from source and needs a compiler.

Run the tests with `.venv/bin/python -m unittest discover -s tests -v`. They need no credentials or network. `tests/test_wiring.py` drives the real Telegram handler wiring; `tests/test_store_contract.py` runs one behavioural contract against the in-memory fake and SQLite stores. The same contract runs against Oracle when `ORACLE_TEST_*` variables point at a **throwaway** database (see the file's docstring); those tests are skipped otherwise and have not been run against the development database.

## Deploy to the OCI bot VM

**Before merging a release that adds database columns, run `scripts/setup_db.py` against the database** (see above). The bot refuses to start on an outdated schema, so a deploy made first would roll back, but nothing is damaged either way.

The [deploy workflow](.github/workflows/deploy.yml) tests pull requests, then deploys each successful push to `master` (or a manual run on `master`). It builds wheels on Ubuntu 22.04/Python 3.10, opens a fresh OCI Bastion restricted to that runner's IP, copies a commit archive through an SSH tunnel, and activates a systemd release. CI never uploads `.env` or the database wallet. A failed service restart restores the previous release. Bastion and its session are deleted after each job.

One-time VM setup, from a trusted workstation that can SSH to the VM as `ubuntu`:

1. Generate a separate deployment SSH key: `ssh-keygen -t ed25519 -N '' -f ~/.ssh/smebot-deploy -C smebot-deploy`. Keep its private half off Git.
2. Copy `deploy/bootstrap_vm.sh`, `deploy/activate_release.sh`, `deploy/smebot.service`, and `~/.ssh/smebot-deploy.pub` to one directory on the VM. Run `sudo bash bootstrap_vm.sh /path/to/smebot-deploy.pub` there. This installs Python venv support and creates separate service and deployment users. For example, from this repository:

   ```sh
   scp deploy/bootstrap_vm.sh deploy/activate_release.sh deploy/smebot.service ~/.ssh/smebot-deploy.pub ubuntu@<bot-public-ip>:/tmp/
   ssh ubuntu@<bot-public-ip> 'sudo bash /tmp/bootstrap_vm.sh /tmp/smebot-deploy.pub'
   ```
3. Install runtime values from [bot.env.example](deploy/bot.env.example) as `/etc/smebot/bot.env`, owned by `root:smebot` with mode `0640`. Put `tnsnames.ora` and `ewallet.pem` in `/etc/smebot/wallet`, owned by `root:smebot` with mode `0640`. Use the existing `SME_BOT` runtime password and wallet password. Set `OCI_AUTH=instance_principal`. Do not put these values in GitHub secrets for deployment.
4. Apply the infrastructure repository's `environments/host/` Terraform stack. It manages the `smebot-dev-bot-runtime` dynamic group matching **only** the bot instance OCID and a policy limited to object create, read, and delete on `smebot-dev-media`. The bot uses its [instance principal](https://docs.oracle.com/en-us/iaas/Content/Identity/callresources/Configuring_the_SDK_CLI_or_Terraform.htm) for photos. Allow several minutes for a new OCI policy to take effect.

Create a GitHub environment named `dev-bot`, restricted to deployments from `master`. Give it these secrets:

| Secret | Purpose |
| --- | --- |
| `OCI_USER_OCID`, `OCI_TENANCY_OCID`, `OCI_FINGERPRINT`, `OCI_API_PRIVATE_KEY` | API key for an OCI IAM user allowed to manage Bastions and sessions in the dev compartment. Use a dedicated CI user. |
| `BOT_DEPLOY_SSH_PRIVATE_KEY` | Private half of the separate deployment key from step 1. |

Set these environment variables (GitHub **vars**):

| Variable | Value |
| --- | --- |
| `BOT_COMPARTMENT_OCID` | `terraform -chdir=environments/host output -raw compartment_ocid` in the infrastructure repo. |
| `BOT_BASTION_SUBNET_OCID` | `terraform -chdir=environments/host output -raw bot_bastion_subnet_ocid`. |
| `BOT_PRIVATE_IP` | `terraform -chdir=environments/host output -raw bot_private_ip`. |
| `BOT_SSH_HOST_KEY` | The VM's trusted SSH host public key, for example `ssh-ed25519 AAAA...`. Obtain it over your existing trusted SSH connection with `ssh ubuntu@<bot-public-ip> 'cat /etc/ssh/ssh_host_ed25519_key.pub'`. |

The CI IAM user needs `manage bastion-family` in the development compartment. The target bot subnet permits SSH only from your workstation CIDR and the dedicated Bastion subnet. The workflow creates a Bastion with the current runner's `/32` allowlist and a one-hour session; it does not open SSH to all GitHub runner addresses. OCI [documents Bastion as free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), though the account's service limits still apply.
