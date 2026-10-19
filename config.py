"""Runtime configuration. Load secrets locally or inject them at deployment time."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from abuse_limits import AbuseLimits
from vault_secrets import load_vault_secrets


load_dotenv()


@dataclass(frozen=True)
class Settings:
    backend: str
    bot_token: str = field(repr=False)
    db_dsn: str
    db_wallet_dir: str
    db_wallet_password: str = field(repr=False)
    db_user: str
    db_password: str = field(repr=False)
    oci_namespace: str
    oci_bucket_name: str
    oci_recovery_bucket: str
    oci_compartment_ocid: str
    oci_instance_ocid: str
    oci_auth: str
    oci_config_file: str
    oci_profile: str
    local_db_path: str
    local_media_dir: str
    abuse_limits: AbuseLimits = field(default_factory=AbuseLimits)
    orders_enabled: bool = False
    seller_panel_url: str = ''
    seller_api_port: int = 8081


def load_settings() -> Settings:
    abuse_limits = AbuseLimits.from_environment(os.environ)
    backend = os.getenv("APP_BACKEND", "oci").lower()
    if backend not in ("oci", "local"):
        raise ValueError("APP_BACKEND must be oci or local")
    secrets_source = os.getenv("SECRETS_SOURCE", "env").lower()
    if secrets_source not in ("env", "oci_vault"):
        raise ValueError("SECRETS_SOURCE must be env or oci_vault")
    if backend == "local" and secrets_source != "env":
        raise ValueError("Local mode requires SECRETS_SOURCE=env")
    auth = os.getenv("OCI_AUTH", "config_file")
    if backend == "oci" and auth not in ("config_file", "instance_principal", "resource_principal"):
        raise ValueError("OCI_AUTH must be config_file, instance_principal, or resource_principal")

    required = (("DB_DSN", "DB_WALLET_DIR", "DB_USER", "OCI_NAMESPACE", "OCI_BUCKET_NAME")
                if backend == "oci" else ())
    if secrets_source == "env":
        required += ("BOT_TOKEN",)
        if backend == "oci":
            required += ("DB_WALLET_PASSWORD", "DB_PASSWORD")
    else:
        if auth != "instance_principal":
            raise ValueError("OCI Vault mode requires OCI_AUTH=instance_principal")
        required += ("BOT_TOKEN_SECRET_OCID", "DB_PASSWORD_SECRET_OCID",
                     "DB_WALLET_PASSWORD_SECRET_OCID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))
    if secrets_source == "oci_vault":
        secret_ids = {
            name: os.environ[f"{name}_SECRET_OCID"]
            for name in ("BOT_TOKEN", "DB_PASSWORD", "DB_WALLET_PASSWORD")
        }
        if any(not secret_id.startswith("ocid1.vaultsecret.") for secret_id in secret_ids.values()):
            raise ValueError("OCI Vault settings must contain Vault secret OCIDs")
        secrets = load_vault_secrets(secret_ids)
    else:
        secrets = {
            "BOT_TOKEN": os.getenv("BOT_TOKEN", ""),
            "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
            "DB_WALLET_PASSWORD": os.getenv("DB_WALLET_PASSWORD", ""),
        }
    orders_enabled = os.getenv("ORDERS_ENABLED", "false").lower()
    if orders_enabled not in ("true", "false"):
        raise ValueError("ORDERS_ENABLED must be true or false")
    local_root = Path(__file__).resolve().parent / ".local"
    return Settings(
        backend=backend,
        abuse_limits=abuse_limits,
        orders_enabled=orders_enabled == "true",
        seller_panel_url=os.getenv('SELLER_PANEL_URL', '').rstrip('/'),
        seller_api_port=int(os.getenv('SELLER_API_PORT', '8081')),
        bot_token=secrets["BOT_TOKEN"],
        db_dsn=os.getenv("DB_DSN", ""),
        db_wallet_dir=os.getenv("DB_WALLET_DIR", ""),
        db_wallet_password=secrets["DB_WALLET_PASSWORD"],
        db_user=os.getenv("DB_USER", ""),
        db_password=secrets["DB_PASSWORD"],
        oci_namespace=os.getenv("OCI_NAMESPACE", ""),
        oci_bucket_name=os.getenv("OCI_BUCKET_NAME", ""),
        oci_recovery_bucket=os.getenv("OCI_RECOVERY_BUCKET", "smebot-dev-recovery"),
        oci_compartment_ocid=os.getenv("OCI_COMPARTMENT_OCID", ""),
        oci_instance_ocid=os.getenv("OCI_INSTANCE_OCID", ""),
        oci_auth=auth,
        oci_config_file=os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config")),
        oci_profile=os.getenv("OCI_PROFILE", "DEFAULT"),
        local_db_path=os.path.expanduser(os.getenv("LOCAL_DB_PATH", str(local_root / "bot.sqlite3"))),
        local_media_dir=os.path.expanduser(os.getenv("LOCAL_MEDIA_DIR", str(local_root / "media"))),
    )
