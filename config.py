"""Runtime configuration. Load secrets locally or inject them at deployment time."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    backend: str
    bot_token: str
    db_dsn: str
    db_wallet_dir: str
    db_wallet_password: str
    db_user: str
    db_password: str
    oci_namespace: str
    oci_bucket_name: str
    oci_auth: str
    oci_config_file: str
    oci_profile: str
    local_db_path: str
    local_media_dir: str


def load_settings() -> Settings:
    backend = os.getenv("APP_BACKEND", "oci").lower()
    if backend not in ("oci", "local"):
        raise ValueError("APP_BACKEND must be oci or local")
    required = ("BOT_TOKEN",)
    if backend == "oci":
        required += (
            "DB_DSN", "DB_WALLET_DIR", "DB_WALLET_PASSWORD",
            "DB_USER", "DB_PASSWORD", "OCI_NAMESPACE", "OCI_BUCKET_NAME",
        )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))
    auth = os.getenv("OCI_AUTH", "config_file")
    if backend == "oci" and auth not in ("config_file", "instance_principal", "resource_principal"):
        raise ValueError("OCI_AUTH must be config_file, instance_principal, or resource_principal")
    local_root = Path(__file__).resolve().parent / ".local"
    return Settings(
        backend=backend,
        bot_token=os.environ["BOT_TOKEN"],
        db_dsn=os.getenv("DB_DSN", ""),
        db_wallet_dir=os.getenv("DB_WALLET_DIR", ""),
        db_wallet_password=os.getenv("DB_WALLET_PASSWORD", ""),
        db_user=os.getenv("DB_USER", ""),
        db_password=os.getenv("DB_PASSWORD", ""),
        oci_namespace=os.getenv("OCI_NAMESPACE", ""),
        oci_bucket_name=os.getenv("OCI_BUCKET_NAME", ""),
        oci_auth=auth,
        oci_config_file=os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config")),
        oci_profile=os.getenv("OCI_PROFILE", "DEFAULT"),
        local_db_path=os.path.expanduser(os.getenv("LOCAL_DB_PATH", str(local_root / "bot.sqlite3"))),
        local_media_dir=os.path.expanduser(os.getenv("LOCAL_MEDIA_DIR", str(local_root / "media"))),
    )
