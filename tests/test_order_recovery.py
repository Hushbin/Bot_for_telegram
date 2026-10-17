"""Five-table recovery with real foreign keys and atomic rollback in SQLite."""
from datetime import datetime, timezone
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_backend import SQLiteStore
from recovery import RecoveryStorage, TABLE_COLUMNS, export_database, restore_database
from test_recovery import FakeObjectClient


class SQLCursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.db.cursor()
        self.rows = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, binds=None):
        self.rows = None
        if sql.startswith(('ALTER SESSION', 'SET TRANSACTION', 'ALTER TABLE SME_SCHEMA')):
            self.rows = []
        elif sql == 'SELECT SYSTIMESTAMP FROM dual':
            self.rows = [(datetime(2026, 10, 15, tzinfo=timezone.utc),)]
        else:
            values = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in (binds or {}).items()}
            self.cursor.execute(sql.replace('SME_SCHEMA.', ''), values)
        return self

    def fetchone(self):
        return self.rows[0] if self.rows is not None else self.cursor.fetchone()

    def __iter__(self):
        return iter(self.rows) if self.rows is not None else iter(self.cursor)

    def setinputsizes(self, **kwargs):
        pass


class SQLConnection:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA foreign_keys=ON')

    def cursor(self):
        return SQLCursor(self)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()


class OrderRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = SQLiteStore(Path(tmp.name) / 'source.db')
        self.target = SQLiteStore(Path(tmp.name) / 'target.db')
        for store in (self.source, self.target):
            with store.connect() as connection:
                for table in ('users', 'sellers', 'products'):
                    for column in ('created_at', 'updated_at'):
                        connection.execute(f'ALTER TABLE {table} ADD {column} TEXT')
        for uid in (1, 2):
            self.source.upsert_user(uid, 'Jane', 'j@example.com', '+1234567890')
        self.source.upsert_seller(2, 'Shop', 's@example.com', 'Online', '+1234567890')
        self.pid = self.source.create_product(2, 'Shirt', 'clothing', None, 1999, 5)
        self.order = self.source.create_order(1, self.source.quote_order(1, self.pid, 2), uuid4().hex)
        self.source.change_order(self.order['order_id'], 2, 'accept', 1)
        self.client = FakeObjectClient()
        self.storage = RecoveryStorage('namespace', 'recovery', client=self.client)
        self.src = SQLConnection(self.source.path)
        self.dst = SQLConnection(self.target.path)
        self.addCleanup(self.src.db.close)
        self.addCleanup(self.dst.db.close)
        self.key = export_database(self.src, self.storage)

    def restore(self, erased=()):
        with patch.object(self.storage, 'erasures_after', return_value=set(erased)):
            return restore_database(self.dst, self.storage, self.key)

    def test_roundtrip_retains_relationships_and_suppresses_unsent(self):
        counts = self.restore()
        self.assertEqual(counts, dict(USERS=2, SELLERS=1, PRODUCTS=1, ORDERS=1, ORDER_NOTIFICATIONS=2))
        self.assertEqual(self.target.get_order(1, 1)['status'], 'ACCEPTED')
        self.assertEqual(self.target.get_product(1)['stock_quantity'], 3)
        self.assertIsNone(self.target.claim_notification())
        self.assertEqual(self.dst.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertGreater(self.target.create_product(2, 'New', 'art', None, 100, 1), 1)

    def test_erased_buyer_cancels_and_restores_stock(self):
        self.restore([1])
        self.assertIsNone(self.target.get_user(1))
        order = self.target.get_order(1, 2)
        self.assertEqual((order['status'], order['buyer_name'], order['buyer_phone']),
                         ('CANCELLED', '[deleted]', '[deleted]'))
        self.assertEqual(self.target.get_product(1)['stock_quantity'], 5)

    def test_erased_seller_keeps_valid_tombstones(self):
        self.restore([2])
        order = self.target.get_order(1, 1)
        self.assertEqual((order['status'], order['seller_name'], order['product_name']),
                         ('CANCELLED', '[deleted]', '[deleted]'))
        self.assertIsNone(self.target.get_product(1))
        self.assertEqual(self.dst.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_both_erased_have_no_visible_history(self):
        self.restore([1, 2])
        self.assertEqual(self.target.list_orders(1), [])
        self.assertEqual(self.target.list_orders(2, True), [])

    def test_count_mismatch_rolls_back_all_tables(self):
        manifest = self.storage.get_json(self.key)
        manifest['counts']['ORDERS'] += 1
        self.storage.put_json(self.key, manifest)
        with self.assertRaisesRegex(ValueError, 'row count'):
            self.restore()
        for table in TABLE_COLUMNS:
            self.assertEqual(self.dst.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_new_erasure_during_restore_aborts(self):
        with patch.object(self.storage, 'erasures_after', side_effect=[set(), {1}]):
            with self.assertRaisesRegex(ValueError, 'markers changed'):
                restore_database(self.dst, self.storage, self.key)
        self.assertEqual(self.dst.db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)

    def test_unknown_format_rejected(self):
        manifest = self.storage.get_json(self.key)
        manifest['format'] = 999
        self.storage.put_json(self.key, manifest)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            self.restore()
