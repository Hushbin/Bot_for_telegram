"""Recovery format, checksums, and erasure replay without OCI credentials."""

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from recovery import RecoveryStorage, TABLE_COLUMNS, LEGACY_TABLE_COLUMNS, export_database, restore_database
from unittest.mock import patch


class FakeObjectClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, namespace, bucket, key, body, **kwargs):
        if kwargs.get("if_none_match") == "*" and key in self.objects:
            raise ValueError("Object exists")
        self.objects[key] = body.read() if hasattr(body, "read") else body

    def get_object(self, namespace, bucket, key):
        data = self.objects[key]
        raw = SimpleNamespace(stream=lambda block_size: iter([data]))
        return SimpleNamespace(data=SimpleNamespace(content=data, raw=raw))

    def list_objects(self, namespace, bucket, **kwargs):
        keys = sorted(k for k in self.objects if k.startswith(kwargs["prefix"]))
        return SimpleNamespace(data=SimpleNamespace(
            objects=[SimpleNamespace(name=k) for k in keys], next_start_with=None))


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, binds=None):
        self.connection.statements.append(sql)
        if sql == "SET TRANSACTION READ ONLY":
            self.result = []
        elif sql == "SELECT SYSTIMESTAMP FROM dual":
            self.result = [(datetime(2026, 10, 15, tzinfo=timezone.utc),)]
        elif sql.startswith("SELECT COUNT(*) FROM SME_SCHEMA."):
            self.result = [(0,)]
        elif sql.startswith("SELECT "):
            table = sql.rsplit(".", 1)[1]
            self.result = self.connection.rows[table]
        elif sql.startswith("INSERT INTO"):
            self.connection.inserted.append((sql, binds))
            self.result = []
        else:
            self.result = []
        return self

    def setinputsizes(self, **sizes):
        pass

    def fetchone(self):
        return self.result[0]

    def __iter__(self):
        return iter(self.result)


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = rows or {table: [] for table in TABLE_COLUMNS}
        self.statements = []
        self.inserted = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeObjectClient()
        self.storage = RecoveryStorage("namespace", "recovery", client=self.client)

    def test_export_then_restore_excludes_post_snapshot_erasure(self):
        def row(table, owner):
            result = {column: None for column in TABLE_COLUMNS[table]}
            result["seller_telegram_user_id" if table == "PRODUCTS" else
                   "telegram_user_id"] = owner
            if table == "PRODUCTS":
                result["product_id"] = owner
            return tuple(result[column] for column in TABLE_COLUMNS[table])

        source = FakeConnection({table: [row(table, 11), row(table, 22)]
                                 for table in LEGACY_TABLE_COLUMNS})
        with patch("recovery.TABLE_COLUMNS", LEGACY_TABLE_COLUMNS), patch("recovery.EXPORT_FORMAT", 1):
            manifest_key = export_database(source, self.storage)
        self.assertIn("SET TRANSACTION READ ONLY", source.statements)
        self.assertEqual(source.rollbacks, 1)
        self.assertEqual(self.storage.get_json("latest.json")["manifest_key"], manifest_key)

        self.client.objects["erasures/11/test.json"] = json.dumps({
            "telegram_user_id": 11, "erased_at": "2026-10-16T00:00:00+00:00"
        }).encode()
        target = FakeConnection()
        counts = restore_database(target, self.storage, manifest_key)
        self.assertEqual(counts, {table: int(table in LEGACY_TABLE_COLUMNS) for table in TABLE_COLUMNS})
        self.assertEqual([binds.get("telegram_user_id", binds.get("seller_telegram_user_id"))
                          for _, binds in target.inserted], [22, 22, 22])
        self.assertEqual(target.commits, 1)
        self.assertIn("START WITH LIMIT VALUE", target.statements[-1])

    def test_checksum_mismatch_aborts_before_import(self):
        key = export_database(FakeConnection(), self.storage)
        data_key = self.storage.get_json(key)["data_key"]
        self.client.objects[data_key] += b"tampered"
        target = FakeConnection()
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore_database(target, self.storage, key)
        self.assertFalse(target.inserted)

    def test_marker_list_failure_aborts_restore(self):
        key = export_database(FakeConnection(), self.storage)
        self.client.list_objects = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("listing unavailable"))
        with self.assertRaisesRegex(RuntimeError, "listing unavailable"):
            restore_database(FakeConnection(), self.storage, key)

    def test_schema_change_retries_a_fresh_snapshot_only(self):
        import oracledb
        error = oracledb.DatabaseError(SimpleNamespace(code=1466))
        connection = FakeConnection()
        with patch("recovery._export_database", side_effect=[error, "exports/test.manifest.json"]) as export, patch("recovery.time.sleep") as pause:
            self.assertEqual(export_database(connection, self.storage), "exports/test.manifest.json")
        self.assertEqual(export.call_count, 2)
        self.assertEqual(connection.rollbacks, 1)
        pause.assert_called_once_with(2)

    def test_schema_change_retries_are_bounded_and_other_errors_propagate(self):
        import oracledb
        for code, expected_calls in ((1466, 4), (942, 1)):
            error = oracledb.DatabaseError(SimpleNamespace(code=code))
            with patch("recovery._export_database", side_effect=error) as export, patch("recovery.time.sleep"):
                with self.assertRaises(oracledb.DatabaseError):
                    export_database(FakeConnection(), self.storage)
            self.assertEqual(export.call_count, expected_calls)

    def test_old_naive_snapshot_is_treated_as_utc(self):
        manifest_key = export_database(FakeConnection(), self.storage)
        manifest = self.storage.get_json(manifest_key)
        manifest["snapshot_at"] = "2026-10-15T00:00:00"
        self.storage.put_json(manifest_key, manifest)
        self.client.objects["erasures/99/test.json"] = json.dumps({
            "telegram_user_id": 99, "erased_at": "2026-10-16T00:00:00+00:00"
        }).encode()
        self.assertEqual(self.storage.erasures_after(
            datetime.fromisoformat(manifest["snapshot_at"])), {99})
