"""Runtime configuration. Load secrets locally or inject them at deployment time."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
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


def load_settings() -> Settings:
    required = (
        "BOT_TOKEN", "DB_DSN", "DB_WALLET_DIR", "DB_WALLET_PASSWORD",
        "DB_USER", "DB_PASSWORD", "OCI_NAMESPACE", "OCI_BUCKET_NAME",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))
    auth = os.getenv("OCI_AUTH", "config_file")
    if auth not in ("config_file", "resource_principal"):
        raise ValueError("OCI_AUTH must be config_file or resource_principal")
    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        db_dsn=os.environ["DB_DSN"],
        db_wallet_dir=os.environ["DB_WALLET_DIR"],
        db_wallet_password=os.environ["DB_WALLET_PASSWORD"],
        db_user=os.environ["DB_USER"],
        db_password=os.environ["DB_PASSWORD"],
        oci_namespace=os.environ["OCI_NAMESPACE"],
        oci_bucket_name=os.environ["OCI_BUCKET_NAME"],
        oci_auth=auth,
        oci_config_file=os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config")),
        oci_profile=os.getenv("OCI_PROFILE", "DEFAULT"),
    )
