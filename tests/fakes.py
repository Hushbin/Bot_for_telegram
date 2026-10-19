"""Disposable SQLite store and in-memory media for handler tests."""
import shutil
import tempfile
import weakref
from pathlib import Path
from local_backend import SQLiteStore


class FakeStore(SQLiteStore):
    def __init__(self):
        directory = tempfile.mkdtemp(prefix="smebot-test-")
        self._cleanup = weakref.finalize(self, shutil.rmtree, directory, True)
        super().__init__(Path(directory) / "bot.sqlite3")
        self.fail_photo_update = False

    def _dictionary(self, table, key):
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        for row in rows:
            row["deleted"] = row["deleted_at"] is not None
            if "phone_verified" in row:
                row["phone_verified"] = bool(row["phone_verified"])
        return {row[key]: row for row in rows}

    @property
    def users(self):
        return self._dictionary("users", "telegram_user_id")

    @property
    def sellers(self):
        return self._dictionary("sellers", "telegram_user_id")

    @property
    def products(self):
        return self._dictionary("products", "product_id")

    def replace_seller_product_photo(self, *args):
        if self.fail_photo_update:
            raise RuntimeError("Database unavailable")
        return super().replace_seller_product_photo(*args)

    def set_product_photo(self, user_id, product_id, key, version_id=None):
        if self.fail_photo_update:
            raise RuntimeError("Database unavailable")
        return super().set_product_photo(user_id, product_id, key, version_id)


class FakeMedia:
    def __init__(self):
        self.objects = {}
        self.fail_delete = False

    def put_product_photo(self, user_id, product_id, raw):
        key = f"products/{user_id}/{product_id}/photo.jpg"
        self.objects[key] = raw
        return key, "test-version"

    def get_photo(self, key):
        return self.objects[key]

    def delete_version(self, key, version_id):
        del self.objects[key]

    def delete_photo(self, key, version_id=None):
        if self.fail_delete:
            raise RuntimeError("Object Storage unavailable")
        self.objects.pop(key, None)
