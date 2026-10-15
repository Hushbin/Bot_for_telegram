"""Private recovery objects and a consistent, bounded-memory Oracle export."""

import gzip
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import oci

from database import SCHEMA


TABLE_COLUMNS = {
    "USERS": ("telegram_user_id", "full_name", "email", "phone", "phone_verified",
              "role", "preferred_category", "deleted_at", "created_at", "updated_at"),
    "SELLERS": ("telegram_user_id", "business_name", "business_email", "address",
                "business_phone", "deleted_at", "created_at", "updated_at"),
    "PRODUCTS": ("product_id", "seller_telegram_user_id", "name", "category",
                 "description", "price_minor", "currency", "stock_quantity",
                 "photo_object_key", "photo_version_id", "deleted_at", "created_at",
                 "updated_at"),
}
TIMESTAMP_COLUMNS = {"deleted_at", "created_at", "updated_at"}


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    """Oracle Thin can return a UTC session timestamp without tzinfo."""
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))


def encode_value(value):
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else str(value)
    return value


class RecoveryStorage:
    def __init__(self, namespace, bucket, *, auth="instance_principal",
                 config_file=None, profile="DEFAULT", client=None):
        if client is None:
            if auth == "instance_principal":
                signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                client = oci.object_storage.ObjectStorageClient({}, signer=signer)
            elif auth == "config_file":
                config = oci.config.from_file(config_file or "~/.oci/config", profile_name=profile)
                client = oci.object_storage.ObjectStorageClient(config)
            else:
                raise ValueError("Recovery requires instance principal or an operator profile")
        self.client = client
        self.namespace = namespace
        self.bucket = bucket

    def put_json(self, key, data, *, create_only=False):
        body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        kwargs = {"content_length": len(body), "content_type": "application/json"}
        if create_only:
            kwargs["if_none_match"] = "*"
        self.client.put_object(self.namespace, self.bucket, key, body, **kwargs)

    def get_json(self, key):
        return json.loads(self.client.get_object(self.namespace, self.bucket, key).data.content)

    def record_erasure(self, telegram_user_id):
        timestamp = utc_now().isoformat()
        key = f"erasures/{int(telegram_user_id)}/{timestamp}-{uuid4().hex}.json"
        self.put_json(key, {"telegram_user_id": int(telegram_user_id),
                            "erased_at": timestamp}, create_only=True)
        return key

    def erasures_after(self, snapshot_at):
        """Fail closed if any marker cannot be listed or read."""
        erased_ids = set()
        start = None
        while True:
            kwargs = {"prefix": "erasures/", "limit": 1000}
            if start:
                kwargs["start"] = start
            page = self.client.list_objects(self.namespace, self.bucket, **kwargs).data
            for item in page.objects:
                marker = self.get_json(item.name)
                if as_utc(datetime.fromisoformat(marker["erased_at"])) > as_utc(snapshot_at):
                    erased_ids.add(int(marker["telegram_user_id"]))
            start = page.next_start_with
            if not start:
                break
        return erased_ids

    def latest_export_age(self):
        latest = self.get_json("latest.json")
        return (utc_now() - as_utc(datetime.fromisoformat(latest["snapshot_at"]))).total_seconds()


def export_database(connection, storage):
    """Read all three tables in one Oracle read-only transaction."""
    export_id = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    data_key = f"exports/{export_id}.jsonl.gz"
    manifest_key = f"exports/{export_id}.manifest.json"
    counts = {name: 0 for name in TABLE_COLUMNS}
    temporary = tempfile.NamedTemporaryFile(prefix="smebot-export-", suffix=".gz", delete=False)
    path = Path(temporary.name)
    temporary.close()
    try:
        with connection.cursor() as cursor, gzip.open(path, "wt", encoding="utf-8") as stream:
            cursor.execute("ALTER SESSION SET TIME_ZONE = 'UTC'")
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT SYSTIMESTAMP FROM dual")
            snapshot_at = as_utc(cursor.fetchone()[0]).isoformat()
            stream.write(json.dumps({"format": 1, "snapshot_at": snapshot_at}) + "\n")
            for table, columns in TABLE_COLUMNS.items():
                cursor.execute(f"SELECT {', '.join(columns)} FROM {SCHEMA}.{table}")
                for row in cursor:
                    values = {column: encode_value(value)
                              for column, value in zip(columns, row)}
                    stream.write(json.dumps({"table": table, "values": values},
                                            separators=(",", ":")) + "\n")
                    counts[table] += 1
        connection.rollback()  # End the read-only transaction.
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        size = path.stat().st_size
        with path.open("rb") as stream:
            storage.client.put_object(storage.namespace, storage.bucket, data_key,
                                      stream, content_length=size,
                                      content_type="application/gzip", if_none_match="*")
        manifest = {"format": 1, "snapshot_at": snapshot_at, "data_key": data_key,
                    "sha256": digest.hexdigest(), "bytes": size, "counts": counts}
        storage.put_json(manifest_key, manifest, create_only=True)
        storage.put_json("latest.json", {"manifest_key": manifest_key,
                                         "snapshot_at": snapshot_at})
        return manifest_key
    finally:
        path.unlink(missing_ok=True)


def restore_database(connection, storage, manifest_key):
    """Import into empty SME_SCHEMA tables only; exclude post-snapshot erasures."""
    if not manifest_key.startswith("exports/") or not manifest_key.endswith(".manifest.json"):
        raise ValueError("Expected an exports/*.manifest.json key")
    manifest = storage.get_json(manifest_key)
    if manifest.get("format") != 1:
        raise ValueError("Unsupported export format")
    snapshot_at = datetime.fromisoformat(manifest["snapshot_at"])
    erased_ids = storage.erasures_after(snapshot_at)
    temporary = tempfile.NamedTemporaryFile(prefix="smebot-restore-", suffix=".gz", delete=False)
    path = Path(temporary.name)
    try:
        response = storage.client.get_object(storage.namespace, storage.bucket,
                                             manifest["data_key"])
        digest = hashlib.sha256()
        size = 0
        for block in response.data.raw.stream(1024 * 1024):
            temporary.write(block)
            digest.update(block)
            size += len(block)
        temporary.close()
        if digest.hexdigest() != manifest["sha256"] or size != manifest["bytes"]:
            raise ValueError("Export checksum or length mismatch")
        with connection.cursor() as cursor:
            for table in TABLE_COLUMNS:
                cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}")
                if cursor.fetchone()[0] != 0:
                    raise ValueError("Restore destination must have empty tables")
            imported = {table: 0 for table in TABLE_COLUMNS}
            seen = {table: 0 for table in TABLE_COLUMNS}
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                header = json.loads(next(stream))
                if header != {"format": 1, "snapshot_at": manifest["snapshot_at"]}:
                    raise ValueError("Export header differs from manifest")
                for line in stream:
                    record = json.loads(line)
                    table = record["table"]
                    columns = TABLE_COLUMNS[table]
                    values = record["values"]
                    seen[table] += 1
                    if set(values) != set(columns):
                        raise ValueError("Export columns differ from schema")
                    owner_id = (values["seller_telegram_user_id"] if table == "PRODUCTS"
                                else values["telegram_user_id"])
                    if int(owner_id) in erased_ids:
                        continue
                    for column in TIMESTAMP_COLUMNS & values.keys():
                        if values[column] is not None:
                            values[column] = datetime.fromisoformat(values[column])
                    placeholders = ", ".join(f":{column}" for column in columns)
                    cursor.execute(
                        f"INSERT INTO {SCHEMA}.{table} ({', '.join(columns)}) "
                        f"VALUES ({placeholders})", values)
                    imported[table] += 1
            if seen != manifest["counts"]:
                raise ValueError("Export row count mismatch")
        connection.commit()
        # DDL commits implicitly in Oracle, so reseed only after the verified data
        # transaction has committed. A failure must keep the bot stopped.
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {SCHEMA}.PRODUCTS MODIFY product_id "
                           "GENERATED BY DEFAULT ON NULL AS IDENTITY (START WITH LIMIT VALUE)")
        return imported
    except Exception:
        connection.rollback()
        raise
    finally:
        temporary.close()
        path.unlink(missing_ok=True)
