"""Durable admission contract, also runnable with isolated ORACLE_TEST_* settings."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from abuse_limits import AbuseLimits, LimitError, midnight
from local_backend import SQLiteStore
from orders import OrderError
import test_store_contract as stores


class PolicyTests(unittest.TestCase):
    def test_defaults_and_validated_overrides(self):
        self.assertEqual(AbuseLimits.from_environment({}), AbuseLimits())
        policy = AbuseLimits.from_environment({'ABUSE_LIMITS_ENABLED': 'true', 'ABUSE_ORDERS_DAILY': '7'})
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.orders_daily, 7)
        for name, value in [('ABUSE_LIMITS_ENABLED', 'yes'), ('ABUSE_PHOTOS_DAILY', '0'),
                            ('ABUSE_ORDER_COOLDOWN', '-1'), ('ABUSE_ACTIVE_LISTINGS', '1.5'),
                            ('ABUSE_LISTINGS_DAILY', '1000001')]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                AbuseLimits.from_environment({name: value})


class AbuseContract:
    def setUp(self):
        self.store = self.make_store()
        self.now = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
        self.store.order_clock = lambda: self.now
        self.buyer = random.SystemRandom().randrange(10**10, 10**15)
        self.seller = self.buyer + 1
        for uid in (self.buyer, self.seller):
            self.store.upsert_user(uid, 'Fixture', 'fixture@example.invalid', '+12025550100')
            self.addCleanup(self.store.erase_user, uid)
        self.store.upsert_seller(self.seller, 'Shop', 'fixture@example.invalid', 'Online', '+12025550101')
        self.pid = self.product()
        self.now += timedelta(seconds=10)
        self.store.abuse_limits = AbuseLimits(enabled=True)

    def product(self, api=False):
        values = dict(name='Fixture', category='art', description='', price_minor=100, stock_quantity=100)
        if api:
            return self.store.save_seller_product(self.seller, values)
        return self.store.create_product(self.seller, **values)

    def order(self, key=None):
        return self.store.create_order(self.buyer, self.store.quote_order(self.buyer, self.pid, 1), key or uuid4().hex)

    def denied(self, code, function, *args):
        with self.assertRaises(LimitError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_listing_shared_daily_budget_and_no_refunds(self):
        self.store.abuse_limits = replace(self.store.abuse_limits, listings_daily=2)
        pid = self.product(True)
        self.store.remove_seller_product(self.seller, pid)
        self.now += timedelta(seconds=10)
        error = self.denied('listing_daily', self.product)
        self.assertEqual(error.retry_after, int((midnight(self.now) + timedelta(days=1) - self.now).total_seconds()))
        self.assertEqual(self.store.abuse_summary(self.seller)['listing_count'], 2)

    def test_cooldown_boundary_and_midnight_reset(self):
        self.product(True)
        self.denied('listing_cooldown', self.product)
        self.now += timedelta(seconds=9, microseconds=999999)
        self.denied('listing_cooldown', self.product)
        self.now += timedelta(microseconds=1)
        self.product()
        self.now = self.now.replace(hour=23, minute=59, second=59)
        self.product()
        self.now = midnight(self.now) + timedelta(days=1)
        self.product(True)
        self.assertEqual(self.store.abuse_summary(self.seller)['listing_count'], 1)

    def test_active_cap_includes_zero_stock_and_grandfathers(self):
        self.product()
        self.store.abuse_limits = replace(self.store.abuse_limits, active_listings=1)
        self.now += timedelta(seconds=10)
        self.store.set_stock(self.seller, self.pid, 0)
        error = self.denied('active_listings', self.product)
        self.assertEqual(error.status, 409)
        self.assertIsNone(error.retry_after)
        for product in self.store.list_products(seller_id=self.seller):
            self.store.remove_seller_product(self.seller, product['product_id'])
        self.product()

    def test_validation_and_failed_insert_do_not_charge(self):
        before = self.store.abuse_summary(self.seller)['listing_count']
        with self.assertRaises(OrderError):
            self.store.save_seller_product(self.seller, {})
        from order_store import OrderDB
        with patch.object(OrderDB, 'insert', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                self.product()
        self.assertEqual(self.store.abuse_summary(self.seller)['listing_count'], before)

    def test_order_idempotency_precedes_cooldown_and_notification(self):
        key = uuid4().hex
        order = self.order(key)
        self.assertEqual(self.order(key)['order_id'], order['order_id'])
        self.denied('order_cooldown', self.order)
        with self.store.order_transaction() as db:
            events = db.one(f"SELECT COUNT(*) AS count FROM {db.table('order_notifications')} WHERE order_id=:oid",
                            oid=order['order_id'])
        self.assertEqual(events['count'], 1)
        self.assertEqual(self.store.abuse_summary(self.buyer)['order_count'], 1)

    def test_order_daily_budget_survives_cancellation(self):
        for _ in range(20):
            order = self.order()
            self.store.change_order(order['order_id'], self.buyer, 'cancel', order['version'])
            self.now += timedelta(seconds=30)
        self.denied('order_daily', self.order)
        self.assertEqual(self.store.abuse_summary(self.buyer)['order_count'], 20)

    def test_pending_seller_cap_excludes_accepted_and_elapsed(self):
        orders = []
        for _ in range(3):
            orders.append(self.order())
            self.now += timedelta(seconds=30)
        self.denied('pending_orders', self.order)
        self.store.change_order(orders[0]['order_id'], self.seller, 'accept', 1)
        self.order()
        self.now += timedelta(days=1)
        # No expiry worker has run; elapsed pending rows must not count.
        self.order()

    def test_total_pending_cap_across_sellers(self):
        self.store.abuse_limits = replace(self.store.abuse_limits, pending_orders=2, pending_per_seller=10)
        for _ in range(2):
            self.order()
            self.now += timedelta(seconds=30)
        self.denied('pending_orders', self.order)

    def test_photos_are_attempts_and_validate_before_admission(self):
        self.store.abuse_limits = replace(self.store.abuse_limits, photos_daily=2)
        with self.assertRaises(OrderError):
            self.store.admit_photo_upload(self.buyer, self.pid)
        with self.assertRaises(OrderError):
            self.store.admit_photo_upload(self.seller, self.pid, 'stale')
        self.store.set_product_photo(self.seller, self.pid, 'fixture.jpg', 'v2')
        with self.assertRaises(OrderError):
            self.store.admit_photo_upload(self.seller, self.pid, 'fixture.jpg', 'v1')
        self.assertEqual(self.store.abuse_summary(self.seller)['photo_attempt_count'], 0)
        self.store.admit_photo_upload(self.seller, self.pid, 'fixture.jpg', 'v2')
        self.denied('photo_cooldown', self.store.admit_photo_upload, self.seller, self.pid, 'fixture.jpg', 'v2')
        self.now += timedelta(seconds=10)
        self.store.admit_photo_upload(self.seller, self.pid, 'fixture.jpg', 'v2')
        self.now += timedelta(seconds=10)
        self.denied('photo_daily', self.store.admit_photo_upload, self.seller, self.pid, 'fixture.jpg', 'v2')
        self.assertEqual(self.store.get_product(self.pid)['photo_object_key'], 'fixture.jpg')

    def test_concurrent_photo_attempts_are_serialized(self):
        self.store.abuse_limits = replace(self.store.abuse_limits, photos_daily=1)
        def admit(_):
            try:
                self.store.admit_photo_upload(self.seller, self.pid)
                return True
            except LimitError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(admit, range(4))), 1)
        self.assertEqual(self.store.abuse_summary(self.seller)['photo_attempt_count'], 1)

    def test_order_transaction_failure_does_not_charge_or_enqueue(self):
        with patch.object(self.store, '_notify', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                self.order()
        self.assertIsNone(self.store.abuse_summary(self.buyer))
        self.assertEqual(self.store.list_orders(self.buyer), [])

    def test_restart_and_reregistration_preserve_budget(self):
        self.product()
        self.store.erase_user(self.seller)
        self.store.upsert_user(self.seller, 'Again', 'fixture@example.invalid', '+12025550101')
        self.store.upsert_seller(self.seller, 'Again', 'fixture@example.invalid', 'Online', '+12025550101')
        self.denied('listing_cooldown', self.product)
        reopened = self.reopen()
        reopened.order_clock = lambda: self.now
        reopened.abuse_limits = self.store.abuse_limits
        self.assertEqual(reopened.abuse_summary(self.seller)['listing_count'], 2)

    def test_sweep_clears_erased_account_and_is_idempotent(self):
        self.store.admit_photo_upload(self.seller, self.pid)
        self.store.erase_user(self.seller)
        self.now = midnight(self.now) + timedelta(days=1)
        self.store.clear_expired_abuse_limits()
        self.assertEqual(self.store.clear_expired_abuse_limits(), 0)
        row = self.store.abuse_summary(self.seller)
        self.assertEqual((row['listing_count'], row['order_count'], row['photo_attempt_count']), (0, 0, 0))
        self.assertIsNone(row['last_photo_at'])

    def test_observation_to_enforcement_retains_counts(self):
        self.store.abuse_limits = AbuseLimits(enabled=False, listings_daily=1)
        for _ in range(3):
            self.product()
        self.store.abuse_limits = replace(self.store.abuse_limits, enabled=True)
        self.denied('listing_daily', self.product)
        self.assertEqual(self.store.abuse_summary(self.seller)['listing_count'], 4)

    def test_restore_barrier_also_applies_in_observation_mode(self):
        self.store.abuse_limits = AbuseLimits()
        until = midnight(self.now) + timedelta(days=1)
        with self.store.order_transaction(True) as db:
            db.execute(f"UPDATE {db.table('abuse_limits')} SET blocked_until=:block_end WHERE telegram_user_id=:account_id",
                       block_end=until, account_id=self.seller)
        self.denied('restore_block', self.product)
        self.denied('restore_block', self.store.admit_photo_upload, self.seller, self.pid)
        self.store.set_stock(self.seller, self.pid, 10)
        self.now = until
        self.product()

    def test_concurrent_listing_entrypoints_cannot_exceed_budget(self):
        self.store.abuse_limits = replace(self.store.abuse_limits, listings_daily=2)
        def create(api):
            try:
                return self.product(api)
            except LimitError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create, [True, False] * 3))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.abuse_summary(self.seller)['listing_count'], 2)

    def test_concurrent_confirmations_admit_one_and_retry_same_key(self):
        self.store.abuse_limits = replace(self.store.abuse_limits, orders_daily=1)
        key = uuid4().hex
        with ThreadPoolExecutor(max_workers=4) as pool:
            orders = list(pool.map(lambda _: self.order(key), range(4)))
        self.assertEqual(len({order['order_id'] for order in orders}), 1)
        self.denied('order_daily', self.order)
        self.assertEqual(self.store.abuse_summary(self.buyer)['order_count'], 1)


class SQLiteAbuseTests(AbuseContract, unittest.TestCase):
    def make_store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return SQLiteStore(Path(temporary.name) / 'test.db')

    def reopen(self):
        return SQLiteStore(self.store.path)


@unittest.skipUnless(os.environ.get('ORACLE_TEST_THROWAWAY') == 'yes' and os.environ.get('ORACLE_TEST_DSN'),
                     'requires isolated ORACLE_TEST_* credentials')
class OracleAbuseTests(AbuseContract, unittest.TestCase):
    def make_store(self):
        return stores.OracleStoreContract.make_store(self)

    def reopen(self):
        return self.make_store()
