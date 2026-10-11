# SMEbot development app

Telegram bot backed by the existing OCI development Autonomous JSON Database and private `smebot-dev-media` Object Storage bucket. This is a clean start: it does not import FaunaDB records or Cloudinary assets. The old `Updating_plan.md` remains historical context.

## Data model

| Table | Purpose | Key data |
| --- | --- | --- |
| `SME_SCHEMA.USERS` | One profile per Telegram user | Telegram user ID, name, email, phone, role, preferred category |
| `SME_SCHEMA.SELLERS` | Business profile for a registered user | Business name, email, address, phone |
| `SME_SCHEMA.PRODUCTS` | Seller catalog and stock | Category, NGN price in kobo, quantity, optional private photo object key |

The bot does not have orders, payments, reservations, or seller approval, so it does not create tables for those features. Product photos stay in Object Storage; the database stores only their private object keys. Customer preference is one column because the current UI chooses one category. A Telegram user ID, rather than a chat ID or a temporary Fauna record ID, identifies a profile across restarts.

`scripts/setup_db.py` creates the `SME_SCHEMA` owner and `SME_BOT` runtime user. The owner has a 20 MB quota and DDL privileges for these tables; the runtime user has only `CREATE SESSION` plus `SELECT`, `INSERT`, and `UPDATE` on the three tables. It has no schema creation, deletion, or ADMIN privileges. The script is rerunnable and skips existing tables and indexes; it does **not** rotate existing user passwords or perform future schema migrations.

## Local setup

Use Python 3.11 or newer. This checkout already has the three empty tables, a protected local wallet, and ignored local database credentials. Add your Telegram token to the ignored `.env` file before starting the bot. For a fresh checkout:

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

The wallet downloader stores only `tnsnames.ora` and `ewallet.pem` with private permissions. The database uses mTLS; [`python-oracledb` Thin mode](https://python-oracledb.readthedocs.io/en/stable/user_guide/connection_handling.html#mutual-tls-mtls-connection-to-oracle-autonomous-database) requires both files and the wallet password. Keep the ignored `.env`, `.setup.env`, and wallet private. A hosted OCI runtime can set `OCI_AUTH=resource_principal` for Object Storage instead of carrying an API key, once its dynamic group and IAM policy are configured.

## Bot commands

All commands run in a private Telegram chat.

| Command | Action |
| --- | --- |
| `/start` | Register once or see the menu on return |
| `/profile` | Update personal details |
| `/sellerprofile` | Create or update a business profile |
| `/preferences` | Choose a catalog category |
| `/addproduct` | Add name, category, NGN price, stock, description, then one optional photo |
| `/photo <ID>` | Attach a photo to one of your products that has none |
| `/products [all\|clothing\|hardware\|food\|art]` | Browse the latest 20 products |
| `/product <ID>` | View details and the private bucket photo if present |
| `/myproducts` | List your latest 20 products |
| `/stock <ID> <quantity>` | Set stock on one of your products |
| `/cancel`, `/skip` | Stop the current conversation, or finish a product without a photo |

The product input format is `name | category | price | stock | description`. A photo must be at most 5 MB. The bot uploads it to a unique private object key and stores the key on the product row. If the database update fails after upload, the bot deletes that exact object version. Photos are never given public Object Storage URLs; `/product` reads the object through the bot's OCI identity and sends it to Telegram.

The bot uses polling for local development. It needs a real Telegram token to run; no token is present in this checkout. A deployment still needs a runtime host, a dedicated OCI identity with only the required bucket object permissions, and secret delivery for `BOT_TOKEN`, `DB_PASSWORD`, and the wallet. The bucket is versioned: future photo bytes and retained versions count against the [Always Free Object Storage allowance](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).
