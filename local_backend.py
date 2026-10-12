"""SQLite and filesystem adapters for developing the bot without OCI."""

import sqlite3
from pathlib import Path
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'CUSTOMER' CHECK (role IN ('CUSTOMER', 'SELLER')),
    preferred_category TEXT
);
CREATE TABLE IF NOT EXISTS sellers (
    telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
    business_name TEXT NOT NULL,
    business_email TEXT NOT NULL,
    address TEXT NOT NULL,
    business_phone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    seller_telegram_user_id INTEGER NOT NULL REFERENCES sellers(telegram_user_id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    price_kobo INTEGER NOT NULL CHECK (price_kobo >= 0),
    stock_quantity INTEGER NOT NULL CHECK (stock_quantity >= 0),
    photo_object_key TEXT
);
CREATE INDEX IF NOT EXISTS products_category_idx ON products(category, product_id);
CREATE INDEX IF NOT EXISTS products_seller_idx ON products(seller_telegram_user_id, product_id);
"""

PRODUCT_COLUMNS = """
    p.product_id, p.seller_telegram_user_id, p.name, p.category,
    p.description, p.price_kobo, p.stock_quantity, p.photo_object_key,
    s.business_name
"""


class SQLiteStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def check(self):
        with self.connect() as connection:
            connection.execute("SELECT COUNT(*) FROM users").fetchone()

    def get_user(self, telegram_user_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_user(self, telegram_user_id, full_name, email, phone):
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO users (telegram_user_id, full_name, email, phone)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    full_name=excluded.full_name, email=excluded.email,
                    phone=excluded.phone
            """, (telegram_user_id, full_name, email, phone))
        return self.get_user(telegram_user_id)

    def set_role(self, telegram_user_id, role):
        if role not in ("CUSTOMER", "SELLER"):
            raise ValueError("Invalid role")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET role = ? WHERE telegram_user_id = ?",
                (role, telegram_user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("User not found")

    def set_preference(self, telegram_user_id, category):
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET preferred_category = ? WHERE telegram_user_id = ?",
                (category, telegram_user_id),
            )

    def upsert_seller(self, telegram_user_id, business_name, business_email,
                      address, business_phone):
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO sellers (telegram_user_id, business_name,
                                     business_email, address, business_phone)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    business_name=excluded.business_name,
                    business_email=excluded.business_email,
                    address=excluded.address,
                    business_phone=excluded.business_phone
            """, (telegram_user_id, business_name, business_email,
                  address, business_phone))
            connection.execute(
                "UPDATE users SET role = 'SELLER' WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )

    def get_seller(self, telegram_user_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sellers WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_product(self, seller_id, name, category, description,
                       price_kobo, stock_quantity):
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT INTO products (seller_telegram_user_id, name, category,
                                      description, price_kobo, stock_quantity)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (seller_id, name, category, description,
                  price_kobo, stock_quantity))
            return cursor.lastrowid

    def set_product_photo(self, seller_id, product_id, object_key):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE products SET photo_object_key = ?
                WHERE product_id = ? AND seller_telegram_user_id = ?
            """, (object_key, product_id, seller_id))
            if cursor.rowcount != 1:
                raise LookupError("Product not found for this seller")

    def set_stock(self, seller_id, product_id, quantity):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE products SET stock_quantity = ?
                WHERE product_id = ? AND seller_telegram_user_id = ?
            """, (quantity, product_id, seller_id))
            return cursor.rowcount == 1

    def get_product(self, product_id):
        with self.connect() as connection:
            row = connection.execute(f"""
                SELECT {PRODUCT_COLUMNS} FROM products p
                JOIN sellers s ON s.telegram_user_id = p.seller_telegram_user_id
                WHERE p.product_id = ?
            """, (product_id,)).fetchone()
            return dict(row) if row else None

    def list_products(self, category=None, seller_id=None):
        where = []
        values = []
        if category is not None:
            where.append("p.category = ?")
            values.append(category)
        if seller_id is not None:
            where.append("p.seller_telegram_user_id = ?")
            values.append(seller_id)
        condition = "WHERE " + " AND ".join(where) if where else ""
        with self.connect() as connection:
            rows = connection.execute(f"""
                SELECT {PRODUCT_COLUMNS} FROM products p
                JOIN sellers s ON s.telegram_user_id = p.seller_telegram_user_id
                {condition} ORDER BY p.product_id DESC LIMIT 20
            """, values).fetchall()
            return [dict(row) for row in rows]


class LocalMediaStorage:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key):
        path = (self.directory / object_key).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("Invalid photo key")
        return path

    def put_product_photo(self, seller_id, product_id, photo):
        if not photo or len(photo) > 5 * 1024 * 1024:
            raise ValueError("Photo must be between 1 byte and 5 MB")
        if photo.startswith(b"\xff\xd8\xff"):
            extension = "jpg"
        elif photo.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        else:
            raise ValueError("Only JPEG or PNG photos are accepted")
        key = f"products/{seller_id}/{product_id}/{uuid4().hex}.{extension}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(photo)
        return key, key  # The key acts as a version token for rollback.

    def get_photo(self, object_key):
        return self._path(object_key).read_bytes()

    def delete_version(self, object_key, version_id):
        if version_id != object_key:
            raise ValueError("Invalid local photo version")
        self._path(object_key).unlink()
