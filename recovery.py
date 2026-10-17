"""Private recovery objects and a consistent, bounded-memory Oracle export."""

import gzip
import hashlib
import json
import secrets
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import oci
import oracledb

from database import SCHEMA
from order_schema import TABLE_COLUMNS as ORDER_TABLE_COLUMNS
from orders import TIME_COLUMNS


LEGACY_TABLE_COLUMNS = {
    "USERS": ("telegram_user_id", "full_name", "email", "phone", "phone_verified",
              "role", "preferred_category", "deleted_at", "created_at", "updated_at"),
    "SELLERS": ("telegram_user_id", "business_name", "business_email", "address",
                "business_phone", "deleted_at", "created_at", "updated_at"),
    "PRODUCTS": ("product_id", "seller_telegram_user_id", "name", "category",
                 "description", "price_minor", "currency", "stock_quantity",
                 "photo_object_key", "photo_version_id", "deleted_at", "created_at",
                 "updated_at"),
}
TABLE_COLUMNS = {**LEGACY_TABLE_COLUMNS, **ORDER_TABLE_COLUMNS}
TIMESTAMP_COLUMNS = TIME_COLUMNS
EXPORT_FORMAT = 2


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
    """Retry recent-DDL snapshot errors without weakening read-only isolation."""
    for attempt in range(4):
        try:
            return _export_database(connection, storage)
        except oracledb.DatabaseError as exc:
            connection.rollback()
            if getattr(exc.args[0], "code", None) != 1466 or attempt == 3:
                raise
            time.sleep(2)


def _export_database(connection, storage):
    """Read all five tables in one Oracle read-only transaction."""
    export_id = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    data_key = f"exports/{export_id}.jsonl.gz"
    manifest_key = f"exports/{export_id}.manifest.json"
    counts = {name: 0 for name in TABLE_COLUMNS}
    temporary = tempfile.NamedTemporaryFile(prefix="smebot-export-", suffix=".gz", delete=False)
    path = Path(temporary.name)
    temporary.close()
    try:
        with connection.cursor() as cursor, gzip.open(path, "wt", encoding="utf-8") as stream:
            cursor.execute("ALTER SESSION SET TIME_ZONE = '+00:00'")
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT SYSTIMESTAMP FROM dual")
            snapshot_at = as_utc(cursor.fetchone()[0]).isoformat()
            stream.write(json.dumps({"format": EXPORT_FORMAT, "snapshot_at": snapshot_at}) + "\n")
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
        manifest = {"format": EXPORT_FORMAT, "snapshot_at": snapshot_at, "data_key": data_key,
                    "sha256": digest.hexdigest(), "bytes": size, "counts": counts}
        storage.put_json(manifest_key, manifest, create_only=True)
        storage.put_json("latest.json", {"manifest_key": manifest_key,
                                         "snapshot_at": snapshot_at})
        return manifest_key
    finally:
        path.unlink(missing_ok=True)


def redact_restored_record(table, values, erased_ids, now):
    """Scrub foreign-key tombstones and return an eligible stock adjustment."""
    erased, stamp, restock = "[deleted]", now.isoformat(), None
    if table in ("USERS", "SELLERS") and int(values["telegram_user_id"]) in erased_ids:
        fields = (("full_name", "email", "phone") if table == "USERS" else
                  ("business_name", "business_email", "address", "business_phone"))
        for field in fields:
            values[field] = erased
        values["deleted_at"] = values["deleted_at"] or stamp
        values["updated_at"] = stamp
        if table == "USERS":
            values.update(phone_verified=0, role="CUSTOMER", preferred_category=None)
    elif table == "PRODUCTS" and int(values["seller_telegram_user_id"]) in erased_ids:
        values.update(name=erased, description=None, stock_quantity=0,
                      photo_object_key=None, photo_version_id=None,
                      deleted_at=values["deleted_at"] or stamp, updated_at=stamp)
    elif table == "ORDERS":
        buyer = int(values["buyer_id"]) in erased_ids
        seller = int(values["seller_id"]) in erased_ids
        if buyer or seller:
            if buyer:
                values.update(buyer_name=erased, buyer_phone=erased, buyer_phone_verified=0,
                              buyer_erased_at=values["buyer_erased_at"] or stamp)
            if seller:
                values.update(seller_name=erased, product_name=erased,
                              seller_erased_at=values["seller_erased_at"] or stamp)
            if values["status"] in ("PENDING", "ACCEPTED"):
                if values["status"] == "ACCEPTED" and buyer and not seller:
                    restock = dict(product_id=values["product_id"], quantity=values["quantity"])
                values.update(status="CANCELLED", cancelled_by=values["buyer_id"] if buyer else values["seller_id"],
                              cancellation_reason="ACCOUNT_ERASED")
            values.update(version=int(values["version"]) + 1, updated_at=stamp)
    elif table == "ORDER_NOTIFICATIONS" and values["state"] in ("PENDING", "SENDING"):
        values.update(state="SUPPRESSED", lease_token=None, lease_until=None, last_error="Restored")
    return restock


def restore_database(connection, storage, manifest_key):
    """Import into empty SME_SCHEMA tables only; exclude post-snapshot erasures."""
    if not manifest_key.startswith("exports/") or not manifest_key.endswith(".manifest.json"):
        raise ValueError("Expected an exports/*.manifest.json key")
    manifest = storage.get_json(manifest_key)
    export_format = manifest.get("format")
    if export_format not in (1, EXPORT_FORMAT):
        raise ValueError("Unsupported export format")
    source_columns = LEGACY_TABLE_COLUMNS if export_format == 1 else TABLE_COLUMNS
    if set(manifest.get("counts", {})) != set(source_columns):
        raise ValueError("Export table set differs from schema")
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
            cursor.execute("ALTER SESSION SET TIME_ZONE = '+00:00'")
            for table in TABLE_COLUMNS:
                cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}")
                if cursor.fetchone()[0] != 0:
                    raise ValueError("Restore destination must have empty tables")
            imported = {table: 0 for table in TABLE_COLUMNS}
            seen = {table: 0 for table in source_columns}
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                header = json.loads(next(stream))
                if header != {"format": export_format, "snapshot_at": manifest["snapshot_at"]}:
                    raise ValueError("Export header differs from manifest")
                last_rank = -1
                table_ranks = {table: rank for rank, table in enumerate(source_columns)}
                for line in stream:
                    record = json.loads(line)
                    table = record["table"]
                    if table not in source_columns:
                        raise ValueError("Unexpected export table")
                    if table_ranks[table] < last_rank:
                        raise ValueError("Export tables are not in dependency order")
                    last_rank = table_ranks[table]
                    columns = source_columns[table]
                    values = record["values"]
                    seen[table] += 1
                    if set(values) != set(columns):
                        raise ValueError("Export columns differ from schema")
                    if export_format == 1:
                        owner_id = (values["seller_telegram_user_id"] if table == "PRODUCTS"
                                    else values["telegram_user_id"])
                        if int(owner_id) in erased_ids:
                            continue
                    else:
                        restock = redact_restored_record(table, values, erased_ids, utc_now())
                        if restock is not None:
                            cursor.execute(
                                f"UPDATE {SCHEMA}.PRODUCTS SET stock_quantity = stock_quantity + :quantity "
                                "WHERE product_id = :product_id AND deleted_at IS NULL", restock)
                    for column in TIMESTAMP_COLUMNS & values.keys():
                        if values[column] is not None:
                            values[column] = datetime.fromisoformat(values[column])
                    cursor.setinputsizes(**{column: oracledb.DB_TYPE_TIMESTAMP_TZ
                                            for column in TIMESTAMP_COLUMNS & values.keys()})
                    insert_columns = columns
                    if table == 'USERS':
                        # The stopped bot must restart with empty session memory.
                        # Also give restored accounts fresh generations, never a
                        # snapshot generation that could match an old credential.
                        values['auth_generation'] = secrets.randbelow(2**62) + 1
                        insert_columns = (*columns, 'auth_generation')
                    placeholders = ", ".join(f":{column}" for column in insert_columns)
                    cursor.execute(
                        f"INSERT INTO {SCHEMA}.{table} ({', '.join(insert_columns)}) "
                        f"VALUES ({placeholders})", values)
                    imported[table] += 1
            if seen != manifest["counts"]:
                raise ValueError("Export row count mismatch")
            if storage.erasures_after(snapshot_at) != erased_ids:
                raise ValueError("Erasure markers changed during restore; retry against an empty destination")
        connection.commit()
        # DDL commits implicitly in Oracle, so reseed only after the verified data
        # transaction has committed. A failure must keep the bot stopped.
        with connection.cursor() as cursor:
            for table, identity in (("PRODUCTS", "product_id"), ("ORDERS", "order_id"),
                                    ("ORDER_NOTIFICATIONS", "event_id")):
                cursor.execute(f"ALTER TABLE {SCHEMA}.{table} MODIFY {identity} "
                               "GENERATED BY DEFAULT ON NULL AS IDENTITY (START WITH LIMIT VALUE)")
        return imported
    except Exception:
        connection.rollback()
        raise
    finally:
        temporary.close()
        path.unlink(missing_ok=True)
