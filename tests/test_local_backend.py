import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_settings
from local_backend import LocalMediaStorage, SQLiteStore


class LocalBackendTests(unittest.TestCase):
    def test_settings_need_only_bot_token(self):
        with patch.dict("os.environ", {"APP_BACKEND": "local", "BOT_TOKEN": "test"}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.backend, "local")
        self.assertEqual(settings.db_dsn, "")
        self.assertEqual(settings.oci_namespace, "")

    def test_catalog_persists_and_photo_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteStore(root / "bot.sqlite3")
            media = LocalMediaStorage(root / "media")
            user = store.upsert_user(42, "Jane", "jane@example.com", "+1234567890")
            self.assertEqual(user["role"], "CUSTOMER")
            store.set_preference(42, "Clothing/Fashion")
            store.upsert_seller(42, "Shop", "shop@example.com", "Street", "+1234567890")
            product_id = store.create_product(42, "Shirt", "Clothing/Fashion", None, 1999, 5)
            key, version = media.put_product_photo(42, product_id, b"\xff\xd8\xfftest")
            store.set_product_photo(42, product_id, key)
            self.assertFalse(store.set_stock(99, product_id, 1))
            self.assertTrue(store.set_stock(42, product_id, 4))

            restarted = SQLiteStore(root / "bot.sqlite3")
            product = restarted.get_product(product_id)
            self.assertEqual(product["business_name"], "Shop")
            self.assertEqual(product["stock_quantity"], 4)
            self.assertEqual(restarted.list_products("Clothing/Fashion")[0]["product_id"], product_id)
            self.assertEqual(media.get_photo(key), b"\xff\xd8\xfftest")
            media.delete_version(key, version)
            self.assertFalse((root / "media" / key).exists())

    def test_photo_key_cannot_escape_local_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            media = LocalMediaStorage(Path(temp) / "media")
            with self.assertRaises(ValueError):
                media.get_photo("../outside")

    def test_delete_photo_removes_the_file_and_tolerates_a_missing_one(self):
        with tempfile.TemporaryDirectory() as temp:
            media = LocalMediaStorage(Path(temp) / "media")
            key, version = media.put_product_photo(42, 1, b"\xff\xd8\xfftest")
            media.delete_photo(key, version)
            self.assertFalse((Path(temp) / "media" / key).exists())
            media.delete_photo(key, version)  # already gone: not an error
            with self.assertRaises(ValueError):
                media.delete_photo(key, "someone-elses-version")

    def test_erase_overwrites_personal_columns_in_the_database_file(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "bot.sqlite3")
            store.upsert_user(42, "Jane Doe", "jane@example.com", "+1234567890", True)
            store.upsert_seller(42, "Shop", "shop@example.com", "Main St", "+1987654321")
            product_id = store.create_product(42, "Secret hat", "Art & Design", "Mine", 100, 3)
            store.set_product_photo(42, product_id, "products/42/1/a.jpg", "v1")
            store.erase_user(42)
            with store.connect() as connection:
                dump = " ".join(str(value) for table in ("users", "sellers", "products")
                                for row in connection.execute(f"SELECT * FROM {table}")
                                for value in tuple(row))
                user = connection.execute("SELECT * FROM users").fetchone()
            for personal in ("Jane", "jane@example.com", "1234567890", "Shop", "Main St",
                             "1987654321", "Secret", "Mine", "products/42"):
                self.assertNotIn(personal, dump)
            self.assertEqual(user["telegram_user_id"], 42)
            self.assertIsNotNone(user["deleted_at"])

    def test_database_created_before_this_release_is_upgraded_in_place(self):
        old_schema = """
            CREATE TABLE users (telegram_user_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
                email TEXT NOT NULL, phone TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'CUSTOMER', preferred_category TEXT);
            CREATE TABLE sellers (telegram_user_id INTEGER PRIMARY KEY, business_name TEXT NOT NULL,
                business_email TEXT NOT NULL, address TEXT NOT NULL, business_phone TEXT NOT NULL);
            CREATE TABLE products (product_id INTEGER PRIMARY KEY,
                seller_telegram_user_id INTEGER NOT NULL, name TEXT NOT NULL,
                category TEXT NOT NULL, description TEXT, price_kobo INTEGER NOT NULL,
                stock_quantity INTEGER NOT NULL, photo_object_key TEXT);
            INSERT INTO users VALUES (7, 'Old User', 'old@example.com', '+111', 'SELLER', NULL);
            INSERT INTO sellers VALUES (7, 'Old Shop', 'o@example.com', 'Old St', '+222');
            INSERT INTO products VALUES (1, 7, 'Old product', 'Art & Design', NULL, 500, 2, NULL);
        """
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(old_schema)
            connection.close()
            store = SQLiteStore(path)
            SQLiteStore(path)  # opening again must be harmless
            user = store.get_user(7)
            self.assertEqual((user["full_name"], user["phone_verified"]), ("Old User", False))
            self.assertEqual(store.get_product(1)["name"], "Old product")
            self.assertEqual(store.erase_user(7), [])
            self.assertIsNone(store.get_user(7))
