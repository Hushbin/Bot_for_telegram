"""Account-generation guards, including erasure racing a protected write."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_backend import SQLiteStore
from orders import OrderError
import test_store_contract as contracts


class SellerAuthContract:
    def setUp(self):
        self.store = self.make_store()
        self.uid = random.SystemRandom().randrange(10**10, 10**15)
        self.buyer = self.uid + 1
        for uid in (self.uid, self.buyer):
            self.addCleanup(self.store.erase_user, uid)
        self.register()
        self.store.upsert_user(self.buyer, 'Buyer', 'b@example.com', '+1234567890')
        self.pid = self.store.create_product(self.uid, 'Original', 'clothing', '', 100, 5)
        self.generation = self.store.seller_auth_generation(self.uid)
        self.values = dict(name='Stale', category='art', description='', price_minor=100,
                           stock_quantity=5, expected_stock=5)

    def register(self):
        self.store.upsert_user(self.uid, 'Seller', 's@example.com', '+1234567890')
        self.store.upsert_seller(self.uid, 'Store', 's@example.com', 'Online', '+1234567890')

    def test_erasure_increments_once_and_reregistration_preserves_generation(self):
        self.store.upsert_user(self.uid, 'Edited', 's@example.com', '+1234567890')
        self.assertEqual(self.store.seller_auth_generation(self.uid), self.generation)
        self.store.erase_user(self.uid)
        self.store.erase_user(self.uid)
        self.register()
        self.assertEqual(self.store.seller_auth_generation(self.uid), self.generation + 1)
        self.store.erase_user(self.uid)
        self.register()
        self.assertEqual(self.store.seller_auth_generation(self.uid), self.generation + 2)

    def test_old_generation_cannot_read_or_mutate_reregistered_account(self):
        self.store.erase_user(self.uid)
        self.register()
        pid = self.store.create_product(self.uid, 'New', 'clothing', '', 100, 5)
        order = self.store.create_order(self.buyer, self.store.quote_order(self.buyer, pid, 1), uuid4().hex)
        calls = [
            lambda: self.store.seller_auth_generation(self.uid, auth_generation=self.generation),
            lambda: self.store.seller_snapshot(self.uid, auth_generation=self.generation),
            lambda: self.store.save_seller_product(self.uid, self.values, auth_generation=self.generation),
            lambda: self.store.save_seller_product(self.uid, self.values, pid, auth_generation=self.generation),
            lambda: self.store.remove_seller_product(self.uid, pid, auth_generation=self.generation),
            lambda: self.store.save_seller_profile(self.uid, dict(business_name='Stale',
                business_email='s@example.com', address='Online', business_phone='+1234567890'),
                auth_generation=self.generation),
            lambda: self.store.replace_seller_product_photo(self.uid, pid, 'stale.jpg', 'v1', None,
                auth_generation=self.generation),
            lambda: self.store.change_order(order['order_id'], self.uid, 'accept', 1,
                auth_generation=self.generation),
        ]
        for index, call in enumerate(calls):
            with self.subTest(operation=index), self.assertRaises(OrderError) as error:
                call()
            self.assertEqual(error.exception.code, 'session')
        self.assertEqual(self.store.get_product(pid)['name'], 'New')
        self.assertEqual(self.store.get_product(pid)['stock_quantity'], 5)
        self.assertEqual(self.store.get_order(order['order_id'], self.uid)['status'], 'PENDING')
        current = self.store.seller_auth_generation(self.uid)
        self.store.save_seller_product(self.uid, self.values, pid, auth_generation=current)
        self.assertEqual(self.store.get_product(pid)['name'], 'Stale')

    def test_erasure_waits_for_authorized_write_then_scrubs_it(self):
        locked, release = threading.Event(), threading.Event()
        erasure_started = threading.Event()
        original = self.store._seller_account
        def pause_after_authorization(db, uid, generation=None):
            result = original(db, uid, generation)
            locked.set()
            if not release.wait(10):
                raise RuntimeError('Test synchronization timed out')
            return result
        def erase_and_register():
            erasure_started.set()
            self.store.erase_user(self.uid)
            self.register()
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(self.store, '_seller_account', side_effect=pause_after_authorization):
                write = pool.submit(self.store.save_seller_product, self.uid, self.values,
                                    auth_generation=self.generation)
                try:
                    self.assertTrue(locked.wait(10))
                    erase = pool.submit(erase_and_register)
                    self.assertTrue(erasure_started.wait(10))
                    with self.assertRaises(FutureTimeout):
                        erase.result(timeout=0.2)
                finally:
                    release.set()
                pid = write.result(timeout=10)
                erase.result(timeout=10)
        self.assertIsNone(self.store.get_product(pid))
        self.assertEqual(self.store.seller_auth_generation(self.uid), self.generation + 1)


class SQLiteSellerAuthTests(SellerAuthContract, unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SQLiteStore(Path(tmp.name) / 'auth.db')

    def test_reopening_does_not_reset_generation(self):
        self.store.erase_user(self.uid)
        self.register()
        reopened = SQLiteStore(self.store.path)
        self.assertEqual(reopened.seller_auth_generation(self.uid), self.generation + 1)


@unittest.skipUnless(os.environ.get('ORACLE_TEST_THROWAWAY') == 'yes' and os.environ.get('ORACLE_TEST_DSN'),
                     'set ORACLE_TEST_* and ORACLE_TEST_THROWAWAY=yes for disposable Oracle')
class OracleSellerAuthTests(SellerAuthContract, unittest.TestCase):
    make_store = contracts.OracleStoreContract.make_store
