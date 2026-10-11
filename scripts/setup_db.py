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
SCHEMA = "SME_SCHEMA"
RUNTIME = "SME_BOT"
TABLES = ("USERS", "SELLERS", "PRODUCTS")


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
