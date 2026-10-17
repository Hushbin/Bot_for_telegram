"""Create the minimal SMEbot schema and a separate, DML-only runtime user.

Run with ADMIN credentials once. Passwords are prompted or read from environment;
they are never printed. This script is safe to rerun and never drops data.
"""

import getpass
import base64
import os
import sys
from pathlib import Path

import oci
import oracledb
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so catalog.py is importable when run as a script

from catalog import LEGACY_CATEGORY_LABELS  # noqa: E402

SCHEMA = "SME_SCHEMA"
RUNTIME = "SME_BOT"
TABLES = ("USERS", "SELLERS", "PRODUCTS", "ORDERS", "ORDER_NOTIFICATIONS")


def password(name: str) -> str:
    value = os.environ.get(name) or getpass.getpass(f"{name}: ")
    if not 16 <= len(value.encode("utf-8")) <= 30 or '"' in value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be 16–30 bytes without quotes or control characters")
    return value


def admin_password() -> str:
    if os.environ.get("DB_ADMIN_PASSWORD"):
        return password("DB_ADMIN_PASSWORD")
    secret_id = os.environ.get("OCI_ADMIN_SECRET_OCID")
    if not secret_id:
        return password("DB_ADMIN_PASSWORD")
    config = oci.config.from_file(
        file_location=os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config")),
        profile_name=os.getenv("OCI_PROFILE", "DEFAULT"),
    )
    bundle = oci.secrets.SecretsClient(config).get_secret_bundle(secret_id).data
    value = base64.b64decode(bundle.secret_bundle_content.content).decode("utf-8")
    if len(value) < 16:
        raise ValueError("ADMIN secret is unexpectedly short")
    return value


def credentials(user: str, secret: str) -> dict:
    wallet_dir = os.environ["DB_WALLET_DIR"]
    wallet_password = os.environ["DB_WALLET_PASSWORD"]
    return dict(
        user=user,
        password=secret,
        dsn=os.environ["DB_DSN"],
        config_dir=wallet_dir,
        wallet_location=wallet_dir,
        wallet_password=wallet_password,
    )


def exists(cursor, username: str) -> bool:
    cursor.execute("SELECT 1 FROM all_users WHERE username = :name", name=username)
    return cursor.fetchone() is not None


def create_user(cursor, username: str, secret: str, *, owner: bool) -> None:
    if exists(cursor, username):
        print(f"{username} already exists; password unchanged")
        return
    quota = "QUOTA 20M ON DATA" if owner else "QUOTA 0 ON DATA"
    cursor.execute(
        f'CREATE USER {username} IDENTIFIED BY "{secret}" DEFAULT TABLESPACE DATA {quota}'
    )
    print(f"Created {username}")


# Columns added after the first release, as (table, column, definition). CREATE TABLE
# in schema.sql covers fresh databases; these bring an existing database up to date.
COLUMN_MIGRATIONS = (
    ("USERS", "PHONE_VERIFIED", "NUMBER(1) DEFAULT 0 NOT NULL CHECK (phone_verified IN (0, 1))"),
    ("USERS", "DELETED_AT", "TIMESTAMP WITH TIME ZONE"),
    ("SELLERS", "DELETED_AT", "TIMESTAMP WITH TIME ZONE"),
    ("PRODUCTS", "DELETED_AT", "TIMESTAMP WITH TIME ZONE"),
    ("PRODUCTS", "PHOTO_VERSION_ID", "VARCHAR2(128 CHAR)"),
    ("PRODUCTS", "TELEGRAM_PHOTO_FILE_ID", "VARCHAR2(1024 CHAR)"),
    ("PRODUCTS", "TELEGRAM_PHOTO_BOT_ID", "NUMBER(19)"),
    ("PRODUCTS", "CURRENCY", "VARCHAR2(3 CHAR) DEFAULT 'USD' NOT NULL"),
)

# Columns renamed after the first release, as (table, old name, new name).
COLUMN_RENAMES = (
    ("PRODUCTS", "PRICE_KOBO", "PRICE_MINOR"),
)

# Columns that held a category label before category keys were introduced.
CATEGORY_COLUMNS = (("PRODUCTS", "CATEGORY"), ("USERS", "PREFERRED_CATEGORY"))


def rename_column_statements(existing: dict[str, set[str]]) -> list[str]:
    """RENAME statements for columns still under their old name.

    A table that is missing, or that already has the new name, is left alone, so a
    second run does nothing. Oracle updates constraints that mention the column.
    """
    return [
        f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"
        for table, old, new in COLUMN_RENAMES
        if old in existing.get(table, ()) and new not in existing[table]
    ]


def category_update_statements() -> list[tuple[str, dict[str, str]]]:
    """(SQL, binds) pairs that turn stored category labels into category keys.

    Each statement only matches rows still holding a label, so it is safe to repeat.
    """
    return [
        (f"UPDATE {table} SET {column} = :key WHERE {column} = :label",
         {"key": key, "label": label})
        for table, column in CATEGORY_COLUMNS
        for label, key in LEGACY_CATEGORY_LABELS.items()
    ]


def missing_column_statements(existing: dict[str, set[str]]) -> list[str]:
    """ALTER statements for migration columns absent from tables that already exist.

    `existing` maps upper-case table name to its upper-case column names. A table
    that does not exist yet is skipped because schema.sql creates it with every column.
    """
    return [
        f"ALTER TABLE {table} ADD ({column} {definition})"
        for table, column, definition in COLUMN_MIGRATIONS
        if table in existing and column not in existing[table]
    ]


def statements() -> list[str]:
    raw = (ROOT / "db" / "schema.sql").read_text()
    raw = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("--"))
    return [statement.strip() for statement in raw.split(";") if statement.strip()]


def main() -> None:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".setup.env")
    admin_secret = admin_password()
    owner_password = password("DB_SCHEMA_PASSWORD")
    runtime_password = password("DB_PASSWORD")
    if len({admin_secret, owner_password, runtime_password}) != 3:
        raise ValueError("Use a different password for each database account")

    with oracledb.connect(**credentials("ADMIN", admin_secret)) as admin:
        with admin.cursor() as cursor:
            create_user(cursor, SCHEMA, owner_password, owner=True)
            create_user(cursor, RUNTIME, runtime_password, owner=False)
            # The product IDENTITY column creates an internal sequence.
            cursor.execute(f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO {SCHEMA}")
            cursor.execute(f"GRANT CREATE SESSION TO {RUNTIME}")

    with oracledb.connect(**credentials(SCHEMA, owner_password)) as owner:
        with owner.cursor() as cursor:
            cursor.execute("SELECT table_name FROM user_tables")
            existing = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT index_name FROM user_indexes")
            indexes = {row[0] for row in cursor.fetchall()}
            for statement in statements():
                words = statement.split()
                if words[:2] == ["CREATE", "TABLE"] and words[2].upper() in existing:
                    continue
                if words[:2] == ["CREATE", "INDEX"] and words[2].upper() in indexes:
                    continue
                cursor.execute(statement)
            cursor.execute("SELECT table_name, column_name FROM user_tab_columns")
            columns: dict[str, set[str]] = {}
            for table, column in cursor.fetchall():
                columns.setdefault(table, set()).add(column)
            for statement in rename_column_statements(columns) + missing_column_statements(columns):
                print(f"Migrating: {statement}")
                cursor.execute(statement)
            for statement, binds in category_update_statements():
                cursor.execute(statement, binds)
                if cursor.rowcount:
                    print(f"Converted {cursor.rowcount} row(s): {binds['label']} -> {binds['key']} "
                          f"({statement.split()[1]})")
            owner.commit()  # the DDL above commits itself; the category UPDATEs do not
            for table in TABLES:
                cursor.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {RUNTIME}")

    with oracledb.connect(**credentials(RUNTIME, runtime_password)) as bot:
        with bot.cursor() as cursor:
            for table in TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}")
                print(f"{table}: {cursor.fetchone()[0]} rows")
    print("Schema ready. Put DB_PASSWORD in your ignored .env; run the bot as SME_BOT.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, oracledb.Error, oci.exceptions.ServiceError) as exc:
        print(f"Database setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
