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
