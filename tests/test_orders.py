"""Order contracts and real multiple-connection inventory races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from fakes import FakeStore
from local_backend import SQLiteStore
from order_store import OrderDB
from orders import OrderError
import test_store_contract as stores


class OrderContract:
    def setUp(self):
        self.store = self.make_store()
        self.buyer = random.SystemRandom().randrange(10**10, 10**15)
        self.seller, self.stranger = self.buyer + 1, self.buyer + 2
        self.now = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
        self.store.order_clock = lambda: self.now
        for uid in (self.buyer, self.seller, self.stranger):
            self.addCleanup(self.store.erase_user, uid)
            self.store.upsert_user(uid, 'Jane', 'j@example.com', '+1234567890')
        self.store.upsert_seller(self.seller, 'Shop', 's@example.com', 'Online', '+1234567891')
        self.pid = self.store.create_product(self.seller, 'Shirt', 'clothing', None, 1999, 5)

    def create(self, quantity=2, key=None):
        return self.store.create_order(self.buyer, self.store.quote_order(self.buyer, self.pid, quantity), key or uuid4().hex)

    def events(self):
        with self.store.order_transaction() as db:
            return db.all(f"SELECT * FROM {db.table('order_notifications')} WHERE order_id IN "
                          f"(SELECT order_id FROM {db.table('orders')} WHERE buyer_id = :account_id)", account_id=self.buyer)

    def test_quote_checks_registration_self_order_and_stock(self):
        for buyer, qty in ((self.buyer + 100, 1), (self.seller, 1), (self.buyer, 0), (self.buyer, 6)):
            with self.assertRaises(OrderError):
                self.store.quote_order(buyer, self.pid, qty)

    def test_confirmation_is_idempotent_and_does_not_reserve_stock(self):
        key = uuid4().hex
        order = self.create(key=key)
        self.assertEqual(self.create(key=key)['order_id'], order['order_id'])
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(order['expires_at'] - order['created_at'], timedelta(hours=24))
        with self.assertRaises(OrderError):
            self.store.create_order(self.stranger, self.store.quote_order(self.stranger, self.pid, 1), key)

    def test_old_confirmation_does_not_reveal_erased_contacts_after_reregistration(self):
        key = uuid4().hex
        self.create(key=key)
        self.store.erase_user(self.buyer)
        self.store.upsert_user(self.buyer, 'New', 'new@example.com', '+1111111111')
        self.assertIsNone(self.store.order_for_key(self.buyer, key))
        with self.assertRaises(OrderError):
            self.create(key=key)
        self.assertEqual(self.store.list_orders(self.buyer), [])

    def test_quote_reconfirmation_includes_price_and_contact_changes(self):
        quote = self.store.quote_order(self.buyer, self.pid, 1)
        self.store.update_product(self.seller, self.pid, 'Shirt', 'clothing', None, 2500, 5)
        with self.assertRaises(OrderError):
            self.store.create_order(self.buyer, quote, uuid4().hex)
        quote = self.store.quote_order(self.buyer, self.pid, 1)
        self.store.upsert_user(self.buyer, 'New name', 'j@example.com', '+1234567890')
        with self.assertRaises(OrderError):
            self.store.create_order(self.buyer, quote, uuid4().hex)

    def test_acceptance_snapshots_and_retries(self):
        order = self.create()
        self.store.update_product(self.seller, self.pid, 'Different', 'art', None, 3000, 5)
        accepted = self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.assertEqual((accepted['status'], accepted['unit_price_minor'], accepted['product_name']), ('ACCEPTED', 1999, 'Shirt'))
        self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 3)
        self.assertEqual(len(self.events()), 2)

    def test_history_and_actions_are_scoped_to_participants(self):
        order = self.create()
        self.assertIsNone(self.store.get_order(order['order_id'], self.stranger))
        self.assertEqual(self.store.list_orders(self.stranger), [])
        for actor, action in ((self.stranger, 'accept'), (self.buyer, 'accept'), (self.buyer, 'decline')):
            with self.assertRaises(OrderError):
                self.store.change_order(order['order_id'], actor, action, 1)
        self.assertEqual(self.store.list_orders(self.seller, True)[0]['order_id'], order['order_id'])

    def test_acceptance_checks_current_stock(self):
        order = self.create()
        self.store.set_stock(self.seller, self.pid, 1)
        with self.assertRaises(OrderError):
            self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.assertEqual(self.store.get_order(order['order_id'], self.buyer)['status'], 'PENDING')
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 1)
        self.assertEqual(len(self.events()), 1)

    def test_acceptance_rolls_back_when_notification_insert_fails(self):
        order = self.create()
        original = OrderDB.insert
        def failing(db, table, values, identity):
            if table == 'order_notifications':
                raise RuntimeError('simulated disk failure')
            return original(db, table, values, identity)
        with patch.object(OrderDB, 'insert', failing), self.assertRaises(RuntimeError):
            self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)
        self.assertEqual(self.store.get_order(order['order_id'], self.buyer)['status'], 'PENDING')

    def test_creation_rolls_back_when_notification_insert_fails(self):
        original = OrderDB.insert
        def failing(db, table, values, identity):
            if table == 'order_notifications':
                raise RuntimeError('simulated disk failure')
            return original(db, table, values, identity)
        with patch.object(OrderDB, 'insert', failing), self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual(self.store.list_orders(self.buyer), [])

    def test_pending_buyer_cancellation_and_seller_decline(self):
        order = self.create()
        self.store.change_order(order['order_id'], self.buyer, 'cancel', 1)
        self.assertEqual(self.store.get_order(order['order_id'], self.seller)['status'], 'CANCELLED')
        other = self.create()
        self.store.change_order(other['order_id'], self.seller, 'decline', 1)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)

    def test_accepted_cancellation_restocks_once_and_requires_seller(self):
        order = self.create()
        self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.assertEqual(self.store.change_order(order['order_id'], self.buyer, 'cancel', 2)['status'], 'ACCEPTED')
        for _ in range(2):
            self.store.change_order(order['order_id'], self.seller, 'cancel', 2)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)

    def test_completion_is_terminal_and_does_not_restore_stock(self):
        order = self.create()
        self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.store.change_order(order['order_id'], self.seller, 'complete', 2)
        self.store.change_order(order['order_id'], self.seller, 'cancel', 3)
        self.assertEqual(self.store.get_order(order['order_id'], self.buyer)['status'], 'COMPLETED')
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 3)

    def test_accept_at_deadline_expires_even_without_worker(self):
        order = self.create()
        self.now = order['expires_at']
        self.assertEqual(self.store.change_order(order['order_id'], self.seller, 'accept', 1)['status'], 'EXPIRED')
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)

    def test_expiry_is_repeatable(self):
        order = self.create()
        self.now += timedelta(days=1)
        self.assertGreaterEqual(self.store.expire_orders(), 1)
        self.store.expire_orders()
        self.assertEqual(self.store.get_order(order['order_id'], self.buyer)['status'], 'EXPIRED')
        self.assertEqual(len(self.events()), 3)

    def test_erasing_buyer_cancels_and_scrubs_without_changing_others(self):
        order = self.create()
        self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.store.erase_user(self.buyer)
        self.store.erase_user(self.buyer)
        saved = self.store.get_order(order['order_id'], self.seller)
        self.assertEqual((saved['status'], saved['buyer_phone'], saved['buyer_name']), ('CANCELLED', '[deleted]', '[deleted]'))
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)
        self.assertIsNone(self.store.get_order(order['order_id'], self.buyer))
        active = [e for e in self.events() if e['state'] == 'PENDING']
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['recipient_id'], self.seller)

    def test_erasing_seller_scrubs_listing_and_prevents_restoring_erased_stock(self):
        order = self.create()
        self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        self.store.erase_user(self.seller)
        saved = self.store.get_order(order['order_id'], self.buyer)
        self.assertEqual((saved['status'], saved['seller_name'], saved['product_name']), ('CANCELLED', '[deleted]', '[deleted]'))
        self.assertIsNone(self.store.get_product(self.pid))

    def test_stock_edits_leave_room_for_cancellation_and_erasure(self):
        order = self.create()
        self.store.change_order(order['order_id'], self.seller, 'accept', 1)
        with self.assertRaises(OrderError):
            self.store.set_stock(self.seller, self.pid, 10**10 - 1)
        with self.assertRaises(OrderError):
            self.store.update_product(self.seller, self.pid, 'Shirt', 'art', None, 100, 10**10 - 1)
        self.store.set_stock(self.seller, self.pid, 10**10 - 3)
        self.store.erase_user(self.buyer)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 10**10 - 1)

    def test_pagination_is_stable(self):
        for _ in range(12):
            self.create(1)
        page = self.store.list_orders(self.buyer, False, 0, 10)
        following = self.store.list_orders(self.buyer, False, 10, 10)
        self.assertEqual((len(page), len(following)), (10, 2))
        self.assertGreater(page[-1]['order_id'], following[0]['order_id'])


class FakeOrderContract(OrderContract, unittest.TestCase):
    def make_store(self):
        return FakeStore()


class SQLiteOrderContract(OrderContract, unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SQLiteStore(Path(tmp.name) / 'bot.sqlite3')


@unittest.skipUnless(os.environ.get('ORACLE_TEST_THROWAWAY') == 'yes' and os.environ.get('ORACLE_TEST_DSN'), 'set ORACLE_TEST_* for Oracle')
class OracleOrderContract(OrderContract, unittest.TestCase):
    def make_store(self):
        return stores.OracleStoreContract.make_store(self)


class InventoryRaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'bot.sqlite3'
        self.store = SQLiteStore(self.path)
        for uid in (1, 2, 3):
            self.store.upsert_user(uid, 'Test', 't@example.com', '+123456789')
        self.store.upsert_seller(2, 'Shop', 's@example.com', 'Online', '+123456789')
        self.pid = self.store.create_product(2, 'Last item', 'art', None, 100, 1)

    def order(self, buyer=1):
        return self.store.create_order(buyer, self.store.quote_order(buyer, self.pid, 1), uuid4().hex)

    def new_connection(self):
        return SQLiteStore(self.path)

    def race(self, first, second):
        barrier = threading.Barrier(2)
        connections = [self.new_connection(), self.new_connection()]
        def run(pair):
            operation, store = pair
            barrier.wait()
            try:
                return operation(store)
            except OrderError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, zip((first, second), connections)))

    def test_only_one_order_gets_last_unit(self):
        a, b = self.order(), self.order(3)
        result = self.race(lambda s: s.change_order(a['order_id'], 2, 'accept', 1), lambda s: s.change_order(b['order_id'], 2, 'accept', 1))
        self.assertEqual(sum(isinstance(r, dict) and r['status'] == 'ACCEPTED' for r in result), 1)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 0)

    def test_confirmation_race_has_one_order_and_event(self):
        quote, key = self.store.quote_order(1, self.pid, 1), uuid4().hex
        result = self.race(lambda s: s.create_order(1, quote, key), lambda s: s.create_order(1, quote, key))
        self.assertEqual(result[0]['order_id'], result[1]['order_id'])
        self.assertEqual(len(self.store.list_orders(1)), 1)

    def test_accept_cancel_race_is_a_valid_serial_outcome(self):
        order = self.order()
        self.race(lambda s: s.change_order(order['order_id'], 2, 'accept', 1), lambda s: s.change_order(order['order_id'], 1, 'cancel', 1))
        saved = self.store.get_order(order['order_id'], 1)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 0 if saved['status'] == 'ACCEPTED' else 1)

    def test_double_cancellation_restores_once(self):
        order = self.order()
        self.store.change_order(order['order_id'], 2, 'accept', 1)
        self.race(lambda s: s.change_order(order['order_id'], 2, 'cancel', 2), lambda s: s.change_order(order['order_id'], 2, 'cancel', 2))
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 1)

    def test_cancellation_stock_edit_race_is_serial(self):
        order = self.order()
        self.store.change_order(order['order_id'], 2, 'accept', 1)
        self.race(lambda s: s.change_order(order['order_id'], 2, 'cancel', 2), lambda s: s.set_stock(2, self.pid, 10))
        self.assertIn(self.store.get_product(self.pid)['stock_quantity'], (10, 11))

    def test_accept_expiry_race_never_both_deducts_and_expires(self):
        order = self.order()
        def early(s):
            s.order_clock = lambda: order['expires_at'] - timedelta(seconds=1)
            return s.change_order(order['order_id'], 2, 'accept', 1)
        def late(s):
            s.order_clock = lambda: order['expires_at']
            return s.change_order(order['order_id'], 2, 'accept', 1)
        self.race(early, late)
        saved = self.store.get_order(order['order_id'], 1)
        self.assertIn(saved['status'], ('ACCEPTED', 'EXPIRED'))
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 0 if saved['status'] == 'ACCEPTED' else 1)

    def test_buyer_erasure_races_acceptance_without_losing_stock(self):
        order = self.order()
        self.race(lambda s: s.change_order(order['order_id'], 2, 'accept', 1), lambda s: s.erase_user(1))
        self.assertEqual(self.store.get_order(order['order_id'], 2)['status'], 'CANCELLED')
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 1)

    def test_creation_races_erasure_without_restoring_erased_contact(self):
        quote = self.store.quote_order(1, self.pid, 1)
        self.race(lambda s: s.create_order(1, quote, uuid4().hex), lambda s: s.erase_user(1))
        self.assertEqual(self.store.list_orders(1), [])
        for order in self.store.list_orders(2, True):
            self.assertEqual(order['buyer_phone'], '[deleted]')
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 1)


@unittest.skipUnless(os.environ.get('ORACLE_TEST_THROWAWAY') == 'yes' and os.environ.get('ORACLE_TEST_DSN'), 'set ORACLE_TEST_* for Oracle races')
class OracleInventoryRaces(InventoryRaces):
    def setUp(self):
        self.store = self.new_connection()
        for uid in (1, 2, 3):
            self.store.upsert_user(uid, 'Test', 't@example.com', '+123456789')
            self.addCleanup(self.store.erase_user, uid)
        self.store.upsert_seller(2, 'Shop', 's@example.com', 'Online', '+123456789')
        self.pid = self.store.create_product(2, 'Last item', 'art', None, 100, 1)

    def new_connection(self):
        return stores.OracleStoreContract.make_store(self)
