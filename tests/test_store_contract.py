"""One behavioural contract, run against every store implementation.

FakeStore (used by handler tests) and SQLiteStore (local development) always run.
OracleStore holds all the production SQL, so it runs the same tests too, but only when
pointed at a THROWAWAY database: these tests write rows and erasure cannot be undone.

    ORACLE_TEST_DSN=host:1521/service ORACLE_TEST_USER=... ORACLE_TEST_PASSWORD=... \
    ORACLE_TEST_SCHEMA=SME_SCHEMA ORACLE_TEST_THROWAWAY=yes \
    python -m unittest discover -s tests -p test_store_contract.py -v

For an mTLS Autonomous Database also set ORACLE_TEST_WALLET_DIR and
ORACLE_TEST_WALLET_PASSWORD. Never point this at the development database that holds
real data. Each test uses fresh random Telegram IDs, so reruns do not collide.
"""

import os
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeStore
from local_backend import SQLiteStore

CLOTHING = "clothing"  # stores hold category keys, never display labels
ART = "art"
_ids = random.SystemRandom()


class StoreContract:
    """Mixin: subclasses provide make_store() and inherit every test."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.uid = _ids.randrange(10**10, 10**15)
        self.other = self.uid + 1

    def register(self, uid=None, seller=False):
        uid = uid or self.uid
        self.store.upsert_user(uid, "Jane Doe", "jane@example.com", "+1234567890")
        if seller:
            self.store.upsert_seller(uid, "Shop", "shop@example.com", "Main St, Town",
                                     "+1234567891")
        return uid

    def product(self, seller_id=None, category=CLOTHING, name="Shirt", stock=5):
        return self.store.create_product(seller_id or self.uid, name, category, "Cotton",
                                         1999, stock)

    # -- users ---------------------------------------------------------------

    def test_unknown_user_is_none(self):
        self.assertIsNone(self.store.get_user(self.uid))

    def test_upsert_user_creates_then_updates_in_place(self):
        created = self.store.upsert_user(self.uid, "Jane", "jane@example.com", "+1111111")
        self.assertEqual(created["role"], "CUSTOMER")
        self.assertIs(created["phone_verified"], False)
        self.assertIsNone(created["preferred_category"])
        updated = self.store.upsert_user(self.uid, "Jane D", "jd@example.com", "+2222222", True)
        self.assertEqual((updated["full_name"], updated["email"], updated["phone"]),
                         ("Jane D", "jd@example.com", "+2222222"))
        self.assertIs(updated["phone_verified"], True)
        self.assertEqual(updated["telegram_user_id"], self.uid)

    def test_signup_role_is_stored_and_profile_edits_preserve_it(self):
        seller = self.store.upsert_user(self.uid, "Jane", "jane@example.com",
                                        "+1111111", False, "SELLER")
        self.assertEqual(seller["role"], "SELLER")
        edited = self.store.upsert_user(self.uid, "Jane D", "jd@example.com", "+2222222")
        self.assertEqual(edited["role"], "SELLER")

    def test_preference_is_stored(self):
        self.register()
        self.store.set_preference(self.uid, ART)
        self.assertEqual(self.store.get_user(self.uid)["preferred_category"], ART)

    def test_category_filter_matches_the_stored_key_exactly(self):
        self.register(seller=True)
        shirt, painting = self.product(category=CLOTHING), self.product(category=ART)
        self.assertEqual([p["product_id"] for p in self.store.list_products(category=ART, seller_id=self.uid)],
                         [painting])
        self.assertEqual([p["product_id"] for p in self.store.list_products(category=CLOTHING, seller_id=self.uid)],
                         [shirt])
        self.assertEqual(self.store.list_products(category="Art & Design"), [])

    # -- sellers -------------------------------------------------------------

    def test_seller_requires_a_registered_user(self):
        with self.assertRaises(Exception):
            self.store.upsert_seller(self.uid, "Shop", "s@example.com", "Street", "+1")

    def test_upsert_seller_promotes_the_user_and_updates_in_place(self):
        self.register()
        self.assertIsNone(self.store.get_seller(self.uid))
        self.store.upsert_seller(self.uid, "Shop", "s@example.com", "Street 1", "+111")
        self.store.upsert_seller(self.uid, "Shop 2", "s2@example.com", "Street 2", "+222")
        seller = self.store.get_seller(self.uid)
        self.assertEqual((seller["business_name"], seller["address"]), ("Shop 2", "Street 2"))
        self.assertEqual(self.store.get_user(self.uid)["role"], "SELLER")

    # -- products ------------------------------------------------------------

    def test_product_needs_a_seller(self):
        self.register()
        with self.assertRaises(Exception):
            self.product()

    def test_product_round_trip_includes_business_name(self):
        self.register(seller=True)
        product_id = self.product()
        found = self.store.get_product(product_id)
        self.assertEqual(found["name"], "Shirt")
        self.assertEqual(found["business_name"], "Shop")
        self.assertEqual((found["price_minor"], found["currency"]), (1999, "USD"))
        self.assertIsNone(found["photo_object_key"])
        self.assertEqual((found["category"], self.store.list_products()[0]["currency"]),
                         (CLOTHING, "USD"))
        self.assertIsNone(self.store.get_product(product_id + 10**6))

    def test_each_seller_has_stable_product_numbers(self):
        self.register(seller=True)
        self.register(self.other, seller=True)
        self.product(self.other, name="Other seller's first")
        first_id, first_number = self.store.create_product(
            self.uid, "Mine", CLOTHING, None, 100, 1, True)
        second_id, second_number = self.store.create_product(
            self.uid, "Mine again", CLOTHING, None, 200, 1, True)
        self.assertEqual((first_number, second_number), (1, 2))
        self.assertEqual(self.store.get_product(first_id)["seller_number"], 1)
        self.assertEqual(self.store.get_product(second_id)["seller_number"], 2)
        self.assertEqual(self.store.get_seller_product(self.uid, 1)["product_id"], first_id)
        self.assertIsNone(self.store.get_seller_product(self.other, 2))
        self.assertEqual([p["seller_number"] for p in self.store.list_products(
            seller_id=self.uid)], [2, 1])

    def test_erased_product_numbers_are_not_reused(self):
        self.register(seller=True)
        _, first_number = self.store.create_product(
            self.uid, "Old", CLOTHING, None, 100, 1, True)
        self.assertEqual(first_number, 1)
        self.store.erase_user(self.uid)
        self.store.upsert_user(self.uid, "Jane", "jane@example.com", "+1234567890")
        self.store.upsert_seller(self.uid, "New shop", "shop@example.com", "Street", "+1")
        _, second_number = self.store.create_product(
            self.uid, "New", CLOTHING, None, 100, 1, True)
        self.assertEqual(second_number, 2)
        self.assertIsNone(self.store.get_seller_product(self.uid, 1))
        self.assertEqual(self.store.get_seller_product(self.uid, 2)["name"], "New")

    def test_only_the_owner_can_change_a_product(self):
        self.register(seller=True)
        self.register(self.other, seller=True)
        product_id = self.product()
        self.assertFalse(self.store.set_stock(self.other, product_id, 1))
        self.assertFalse(self.store.update_product(
            self.other, product_id, "Hijacked", CLOTHING, None, 1, 1))
        with self.assertRaises(LookupError):
            self.store.set_product_photo(self.other, product_id, "k", "v")
        self.assertTrue(self.store.set_stock(self.uid, product_id, 2))
        self.assertTrue(self.store.update_product(
            self.uid, product_id, "Blue shirt", ART, None, 2500, 7))
        self.store.set_product_photo(self.uid, product_id, "products/a.jpg", "v1")
        found = self.store.get_product(product_id)
        self.assertEqual((found["name"], found["category"], found["description"]),
                         ("Blue shirt", ART, None))
        self.assertEqual((found["price_minor"], found["stock_quantity"]), (2500, 7))
        self.assertEqual(found["photo_object_key"], "products/a.jpg")

    def test_list_filters_orders_newest_first_and_caps_at_twenty(self):
        self.register(seller=True)
        self.register(self.other, seller=True)
        ids = [self.product(name=f"P{n}", category=CLOTHING if n % 2 else ART)
               for n in range(22)]
        theirs = self.product(self.other, name="Theirs")
        mine = self.store.list_products(seller_id=self.uid)
        self.assertEqual([p["product_id"] for p in mine], sorted(ids, reverse=True)[:20])
        self.assertNotIn(theirs, [p["product_id"] for p in mine])
        art = self.store.list_products(category=ART, seller_id=self.uid)
        self.assertTrue(art and all(p["category"] == ART for p in art))
        self.assertEqual(self.store.count_products(self.uid), 22)
        self.assertEqual(self.store.count_products(self.other), 1)

    # -- erasure -------------------------------------------------------------

    def test_erase_hides_everything_and_returns_the_photos(self):
        self.register(seller=True)
        with_photo, without_photo = self.product(), self.product(name="Hat")
        self.store.set_product_photo(self.uid, with_photo, "products/a.jpg", "v1")
        self.assertEqual(self.store.erase_user(self.uid), [("products/a.jpg", "v1")])
        self.assertIsNone(self.store.get_user(self.uid))
        self.assertIsNone(self.store.get_seller(self.uid))
        self.assertIsNone(self.store.get_product(with_photo))
        self.assertIsNone(self.store.get_product(without_photo))
        self.assertEqual(self.store.list_products(seller_id=self.uid), [])
        self.assertEqual(self.store.count_products(self.uid), 0)
        self.assertFalse(self.store.set_stock(self.uid, without_photo, 1))

    def test_erase_is_idempotent_and_ignores_unknown_users(self):
        self.register(seller=True)
        self.assertEqual(self.store.erase_user(self.uid), [])
        self.assertEqual(self.store.erase_user(self.uid), [])
        self.assertEqual(self.store.erase_user(self.other), [])

    def test_erase_leaves_other_users_alone(self):
        self.register(seller=True)
        self.register(self.other, seller=True)
        mine, theirs = self.product(), self.product(self.other, name="Theirs")
        self.store.erase_user(self.uid)
        self.assertEqual(self.store.get_user(self.other)["full_name"], "Jane Doe")
        self.assertEqual(self.store.get_product(theirs)["name"], "Theirs")
        self.assertIsNone(self.store.get_product(mine))

    def test_a_user_can_register_again_after_erasure(self):
        self.register(seller=True)
        old = self.product()
        self.store.erase_user(self.uid)
        again = self.store.upsert_user(self.uid, "New Name", "new@example.com", "+333", True)
        self.assertEqual((again["full_name"], again["role"]), ("New Name", "CUSTOMER"))
        self.assertIsNone(self.store.get_seller(self.uid))
        self.assertIsNone(self.store.get_product(old))
        self.store.upsert_seller(self.uid, "New Shop", "n@example.com", "New St", "+444")
        fresh = self.product(name="Fresh")
        self.assertEqual([p["product_id"] for p in self.store.list_products(seller_id=self.uid)],
                         [fresh])


class FakeStoreContract(StoreContract, unittest.TestCase):
    def make_store(self):
        return FakeStore()


class SQLiteStoreContract(StoreContract, unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SQLiteStore(Path(tmp.name) / "bot.sqlite3")


@unittest.skipUnless(os.environ.get("ORACLE_TEST_THROWAWAY") == "yes"
                     and os.environ.get("ORACLE_TEST_DSN"),
                     "set ORACLE_TEST_* and ORACLE_TEST_THROWAWAY=yes to run against Oracle")
class OracleStoreContract(StoreContract, unittest.TestCase):
    def make_store(self):
        from database import OracleStore
        env = os.environ
        return OracleStore(SimpleNamespace(
            db_user=env["ORACLE_TEST_USER"], db_password=env["ORACLE_TEST_PASSWORD"],
            db_dsn=env["ORACLE_TEST_DSN"], db_schema=env.get("ORACLE_TEST_SCHEMA", ""),
            db_wallet_dir=env.get("ORACLE_TEST_WALLET_DIR", ""),
            db_wallet_password=env.get("ORACLE_TEST_WALLET_PASSWORD", "")))


if __name__ == "__main__":
    unittest.main()
