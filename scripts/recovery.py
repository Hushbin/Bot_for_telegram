"""VM export/health jobs and operator-only import into an empty Oracle schema."""

import argparse
import getpass
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import oci  # noqa: E402
import oracledb  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from config import load_settings  # noqa: E402
from database import OracleStore, SCHEMA  # noqa: E402
from recovery import RecoveryStorage, export_database, restore_database  # noqa: E402
from readiness import collect_report  # noqa: E402


def vm_storage(settings):
    return RecoveryStorage(settings.oci_namespace, settings.oci_recovery_bucket,
                           auth="instance_principal")


def export_job():
    settings = load_settings()
    if settings.backend != "oci" or settings.oci_auth != "instance_principal":
        raise ValueError("The scheduled export requires OCI VM mode")
    store = OracleStore(settings)
    with store.connect() as connection:
        key = export_database(connection, vm_storage(settings))
    print(f"Completed logical export: {key}")


def publish_health(values, compartment_id, instance_id):
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    # PostMetricData has a separate ingestion endpoint. The normal telemetry
    # endpoint returns 404 even when the instance principal has METRIC_WRITE.
    client = oci.monitoring.MonitoringClient(
        {}, signer=signer,
        service_endpoint=f"https://telemetry-ingestion.{signer.region}.oraclecloud.com",
    )
    timestamp = datetime.now(timezone.utc)
    metrics = [oci.monitoring.models.MetricDataDetails(
        compartment_id=compartment_id, namespace="smebot_health", name="Healthy",
        dimensions={"resourceId": instance_id, "check": check},
        datapoints=[oci.monitoring.models.Datapoint(timestamp=timestamp, value=float(ok))],
    ) for check, ok in values.items()]
    client.post_metric_data(oci.monitoring.models.PostMetricDataDetails(metric_data=metrics))


def health_job():
    compartment_id = os.environ["OCI_COMPARTMENT_OCID"]
    instance_id = os.environ["OCI_INSTANCE_OCID"]
    values = {"service": False, "telegram": False, "database": False,
              "backup": False}
    try:
        values["service"] = subprocess.run(
            ["systemctl", "is-active", "--quiet", "smebot.service"], check=False,
            timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    report = collect_report(ROOT.name)
    for name in ('telegram', 'database', 'backup'):
        values[name] = report['checks'][name]['status'] == 'pass'
    publish_health(values, compartment_id, instance_id)
    print("Health: " + ", ".join(f"{name}={int(ok)}" for name, ok in values.items()))
    return 0 if all(values.values()) else 1


def restore_job(manifest_key):
    # A restore is an operator task run from the laptop against a new, empty ADB.
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".setup.env")
    required = ("OCI_NAMESPACE", "DB_DSN", "DB_WALLET_DIR", "DB_WALLET_PASSWORD")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing restore settings: " + ", ".join(missing))
    owner_password = os.getenv("DB_SCHEMA_PASSWORD") or getpass.getpass("DB_SCHEMA_PASSWORD: ")
    storage = RecoveryStorage(
        os.environ["OCI_NAMESPACE"], os.getenv("OCI_RECOVERY_BUCKET", "smebot-dev-recovery"),
        auth="config_file", config_file=os.path.expanduser(
            os.getenv("OCI_CONFIG_FILE", "~/.oci/config")),
        profile=os.getenv("OCI_PROFILE", "DEFAULT"),
    )
    with oracledb.connect(
        user=SCHEMA, password=owner_password, dsn=os.environ["DB_DSN"],
        config_dir=os.environ["DB_WALLET_DIR"],
        wallet_location=os.environ["DB_WALLET_DIR"],
        wallet_password=os.environ["DB_WALLET_PASSWORD"],
    ) as connection:
        counts = restore_database(connection, storage, manifest_key)
    print("Restored rows: " + ", ".join(f"{table}={count}" for table, count in counts.items()))
    print("Review redacted orders, reconciled stock and suppressed notifications before starting the bot. "
          "Activity after the selected snapshot may be missing; keep the old bot stopped during validation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export")
    commands.add_parser("health")
    restore = commands.add_parser("restore")
    restore.add_argument("manifest_key")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "export":
        export_job()
    elif args.command == "health":
        return health_job()
    else:
        restore_job(args.manifest_key)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # SDK/HTTP exception text can contain a request URL with the bot token.
        print(f"Recovery task failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
