"""Shared management and media contracts with optional isolated Oracle execution."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import random
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from abuse_limits import AbuseLimits
from fakes import FakeMedia
from local_backend import SQLiteStore
from orders import OrderError
from product_media import remove_product, save_product_photo
from seller_store import product_snapshot
import test_store_contract as stores


class ManagementContract:
    def setUp(self):
        self.store = self.make_store()
        self.uid = random.SystemRandom().randrange(10**10, 10**15)
        self.buyer, self.other = self.uid + 1, self.uid + 2
        self.now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        self.store.order_clock = lambda: self.now
        for uid in (self.uid, self.buyer, self.other):
            self.store.upsert_user(uid, 'Fixture', 'fixture@example.invalid', '+12025550100')
            self.addCleanup(self.store.erase_user, uid)
        self.store.upsert_seller(self.uid, 'Fixture shop', 'fixture@example.invalid', 'Online', '+12025550100')
        self.pid = self.store.create_product(self.uid, 'Original', 'art', 'Original description', 100, 5)
        self.media = FakeMedia()

    def edit(self, field, value, expected):
        return self.store.edit_seller_product_field(self.uid, self.pid, field, value, expected)

    def order(self):
        return self.store.create_order(self.buyer, self.store.quote_order(self.buyer, self.pid, 2), uuid4().hex)

    def row(self):
        with self.store.order_transaction() as db:
            return self.store._product(db, self.pid)

    def photo(self, version='v1'):
        self.store.set_product_photo(self.uid, self.pid, 'old.jpg', version)
        self.media.objects['old.jpg'] = b'old'
        self.store.cache_product_photo(self.pid, 'old.jpg', version, 7, 'cached')

    def test_edits_only_selected_field_and_preserves_other_changes(self):
        self.edit('name', 'Changed', 'Original')
        self.edit('description', None, 'Original description')
        self.edit('category', 'clothing', 'art')
        self.edit('price_minor', 250, 100)
        self.edit('stock_quantity', 8, 5)
        row = self.row()
        self.assertEqual(tuple(row[k] for k in ('name','description','category','price_minor','stock_quantity')),
                         ('Changed', None, 'clothing', 250, 8))
        with self.assertRaises(OrderError):
            self.edit('name', 'Stale', 'Original')

    def test_invalid_fields_values_and_ownership_never_write(self):
        for field, value in [('unknown', 'x'), ('name', ''), ('price_minor', -1),
                             ('stock_quantity', True), ('category', 'missing'), ('description', 'x'*1001)]:
            with self.subTest(field=field), self.assertRaises(OrderError):
                self.edit(field, value, self.row().get(field))
        with self.assertRaises(OrderError):
            self.store.edit_seller_product_field(self.other, self.pid, 'name', 'Forged', 'Original')
        self.assertEqual(self.row()['name'], 'Original')

    def test_concurrent_same_field_updates_have_one_winner(self):
        gate = threading.Barrier(2)
        def attempt(value):
            gate.wait()
            try:
                self.edit('name', value, 'Original')
                return True
            except OrderError as exc:
                self.assertEqual(exc.code, 'conflict')
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, ['First', 'Second'])), 1)

    def test_stock_conflict_and_return_capacity_remain_protected(self):
        order = self.order()
        self.store.change_order(order['order_id'], self.uid, 'accept', 1)
        with self.assertRaises(OrderError):
            self.edit('stock_quantity', 100, 5)
        with self.assertRaises(OrderError):
            self.edit('stock_quantity', 10**10-1, 3)
        self.assertEqual(self.row()['stock_quantity'], 3)

    def test_existing_order_snapshot_is_unchanged_by_edits(self):
        order = self.order()
        self.edit('name', 'New name', 'Original')
        self.edit('price_minor', 999, 100)
        saved = self.store.get_order(order['order_id'], self.buyer)
        self.assertEqual((saved['product_name'], saved['unit_price_minor']), ('Original', 100))

    def test_pending_blocks_but_elapsed_pending_does_not(self):
        order = self.order()
        with self.assertRaises(OrderError) as caught:
            self.store.remove_seller_product(self.uid, self.pid)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertIn('/sales', str(caught.exception))
        self.now = order['expires_at']
        self.store.remove_seller_product(self.uid, self.pid)
        self.assertIsNone(self.store.get_product(self.pid))
        self.assertEqual(self.store.get_order(order['order_id'], self.buyer)['status'], 'PENDING')

    def test_accepted_blocks_until_completed_even_after_expiry_time(self):
        order = self.order()
        self.store.change_order(order['order_id'], self.uid, 'accept', 1)
        self.now += timedelta(days=3)
        with self.assertRaises(OrderError):
            self.store.remove_seller_product(self.uid, self.pid)
        self.store.change_order(order['order_id'], self.uid, 'complete', 2)
        self.store.remove_seller_product(self.uid, self.pid)
        self.assertEqual(self.store.get_order(order['order_id'], self.buyer)['status'], 'COMPLETED')

    def test_stale_removal_confirmation_and_foreign_actor_fail(self):
        snapshot = product_snapshot(self.row())
        self.edit('name', 'Changed', 'Original')
        with self.assertRaises(OrderError):
            self.store.remove_seller_product(self.uid, self.pid, snapshot)
        with self.assertRaises(OrderError):
            self.store.remove_seller_product(self.other, self.pid)
        self.assertIsNone(self.row()['deleted_at'])

    def test_deletion_cleans_exact_photo_and_keeps_numbering(self):
        self.photo()
        remove_product(self.store, self.media, self.uid, self.pid, product_snapshot(self.row()))
        self.assertNotIn('old.jpg', self.media.objects)
        self.assertIsNone(self.row()['photo_object_key'])
        self.assertIsNone(self.row()['telegram_photo_file_id'])
        self.assertFalse(self.store.cache_product_photo(self.pid, 'old.jpg', 'v1', 7, 'late'))
        remove_product(self.store, self.media, self.uid, self.pid)
        pid, number = self.store.create_product(self.uid, 'Next', 'art', None, 100, 1, True)
        self.assertEqual(number, 2)
        self.assertIsNone(self.store.get_seller_product(self.uid, 1))
        self.assertEqual(self.store.get_seller_product(self.uid, 2)['product_id'], pid)

    def test_failed_cleanup_retains_reference_and_repeat_removal_retries(self):
        self.photo()
        self.media.fail_delete = True
        with self.assertLogs('product_media', level='ERROR'):
            remove_product(self.store, self.media, self.uid, self.pid)
        self.assertIsNone(self.store.get_product(self.pid))
        self.assertEqual(self.row()['photo_object_key'], 'old.jpg')
        self.media.fail_delete = False
        remove_product(self.store, self.media, self.uid, self.pid)
        self.assertIsNone(self.row()['photo_object_key'])
        self.assertEqual(self.media.objects, {})

    def test_unknown_photo_version_retains_cleanup_metadata(self):
        self.photo(None)
        with self.assertLogs('product_media', level='WARNING'):
            remove_product(self.store, self.media, self.uid, self.pid)
        self.assertEqual(self.row()['photo_object_key'], 'old.jpg')
        self.assertIsNone(self.row()['photo_version_id'])

    def test_cleanup_compare_and_clear_is_conditional(self):
        self.photo()
        self.store.remove_seller_product(self.uid, self.pid)
        self.assertFalse(self.store.clear_removed_product_photo(self.other, self.pid, 'old.jpg', 'v1'))
        self.assertFalse(self.store.clear_removed_product_photo(self.uid, self.pid, 'old.jpg', 'wrong'))
        self.assertTrue(self.store.clear_removed_product_photo(self.uid, self.pid, 'old.jpg', 'v1'))

    def test_photo_replacement_is_revision_checked_and_invalidates_cache(self):
        self.photo()
        with self.assertRaises(OrderError):
            self.store.replace_seller_product_photo(self.uid, self.pid, 'other.jpg', 'v3', 'old.jpg', 'wrong')
        key, version = save_product_photo(self.store, self.media, self.uid, self.pid, b'new', 'old.jpg', 'v1')
        self.assertNotIn('old.jpg', self.media.objects)
        self.assertEqual((self.row()['photo_object_key'], self.row()['photo_version_id']), (key, version))
        self.assertIsNone(self.row()['telegram_photo_file_id'])

    def test_failed_attach_removes_upload_and_preserves_original(self):
        self.photo()
        with patch.object(self.store, 'replace_seller_product_photo', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                save_product_photo(self.store, self.media, self.uid, self.pid, b'new', 'old.jpg', 'v1')
        self.assertEqual(self.media.objects, {'old.jpg': b'old'})
        self.assertEqual(self.row()['photo_object_key'], 'old.jpg')

    def test_edit_and_removal_work_when_listing_budget_is_exhausted(self):
        self.store.abuse_limits = AbuseLimits(enabled=True, listings_daily=1)
        self.edit('name', 'Still editable', 'Original')
        self.store.remove_seller_product(self.uid, self.pid)
        self.assertEqual(self.store.abuse_summary(self.uid)['listing_count'], 1)

    def test_removal_racing_new_order_cannot_strand_an_order(self):
        quote = self.store.quote_order(self.buyer, self.pid, 1)
        gate = threading.Barrier(2)
        def remove():
            gate.wait()
            try:
                self.store.remove_seller_product(self.uid, self.pid)
                return 'removed'
            except OrderError:
                return 'blocked'
        def order():
            gate.wait()
            try:
                self.store.create_order(self.buyer, quote, uuid4().hex)
                return 'ordered'
            except OrderError:
                return 'unavailable'
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(remove), pool.submit(order)
            result = (a.result(), b.result())
        self.assertIn(result, [('removed', 'unavailable'), ('blocked', 'ordered')])


class SQLiteManagementContract(ManagementContract, unittest.TestCase):
    def make_store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return SQLiteStore(Path(temporary.name) / 'test.db')


@unittest.skipUnless(os.environ.get('ORACLE_TEST_THROWAWAY') == 'yes' and os.environ.get('ORACLE_TEST_DSN'),
                     'requires isolated ORACLE_TEST_* credentials')
class OracleManagementContract(ManagementContract, unittest.TestCase):
    def make_store(self):
        return stores.OracleStoreContract.make_store(self)
