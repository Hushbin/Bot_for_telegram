"""SQLite and filesystem adapters for developing the bot without OCI."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from catalog import LEGACY_CATEGORY_LABELS, PRODUCT_PAGE_SIZE, validate_product_cursor
from order_store import OrderDB, OrderStoreMixin
from seller_store import SellerStoreMixin
from abuse_schema import SCHEMA as ABUSE_SCHEMA, TABLE_COLUMNS as ABUSE_TABLE_COLUMNS
from order_schema import SCHEMA as ORDER_SCHEMA, TABLE_COLUMNS as ORDER_TABLE_COLUMNS


ERASED = "[deleted]"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    phone_verified INTEGER NOT NULL DEFAULT 0 CHECK (phone_verified IN (0, 1)),
    role TEXT NOT NULL DEFAULT 'CUSTOMER' CHECK (role IN ('CUSTOMER', 'SELLER')),
    preferred_category TEXT,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS sellers (
    telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
    business_name TEXT NOT NULL,
    business_email TEXT NOT NULL,
    address TEXT NOT NULL,
    business_phone TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    seller_telegram_user_id INTEGER NOT NULL REFERENCES sellers(telegram_user_id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    stock_quantity INTEGER NOT NULL CHECK (stock_quantity >= 0),
    photo_object_key TEXT,
    photo_version_id TEXT,
    telegram_photo_file_id TEXT,
    telegram_photo_bot_id INTEGER,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS products_category_idx ON products(category, product_id);
CREATE INDEX IF NOT EXISTS products_seller_idx ON products(seller_telegram_user_id, product_id);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an existing
# .local/bot.sqlite3 untouched, so these are added in place when missing.
COLUMN_MIGRATIONS = (
    ("users", "phone_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "deleted_at", "TEXT"),
    ("sellers", "deleted_at", "TEXT"),
    ("products", "deleted_at", "TEXT"),
    ("products", "photo_version_id", "TEXT"),
    ("products", "telegram_photo_file_id", "TEXT"),
    ("products", "telegram_photo_bot_id", "INTEGER"),
    ("products", "currency", "TEXT NOT NULL DEFAULT 'USD'"),
)

# Columns renamed after the first release: (table, old name, new name).
COLUMN_RENAMES = (
    ("products", "price_kobo", "price_minor"),
)

# Columns that held a category label before keys were introduced.
CATEGORY_COLUMNS = (("products", "category"), ("users", "preferred_category"))

USER_COLUMNS = "telegram_user_id, full_name, email, phone, phone_verified, role, preferred_category"
SELLER_COLUMNS = "telegram_user_id, business_name, business_email, address, business_phone"
PRODUCT_COLUMNS = """
    p.product_id, p.seller_telegram_user_id,
    (SELECT COUNT(*) FROM products earlier
     WHERE earlier.seller_telegram_user_id = p.seller_telegram_user_id
       AND earlier.product_id <= p.product_id) AS seller_number,
    p.name, p.category,
    p.description, p.price_minor, p.currency, p.stock_quantity, p.photo_object_key,
    s.business_name
"""


SCHEMA += ORDER_SCHEMA + ABUSE_SCHEMA


class SQLiteStore(SellerStoreMixin, OrderStoreMixin):
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self.migrate(connection)

    @staticmethod
    def migrate(connection):
        """Bring a database file created by an earlier release up to date, in place.

        Every step checks before acting, so running it on a current database is a no-op.
        """
        def columns(table):
            return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

        for table, old, new in COLUMN_RENAMES:
            if old in columns(table) and new not in columns(table):
                connection.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        for table, column, definition in COLUMN_MIGRATIONS:
            if column not in columns(table):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        for table, column in CATEGORY_COLUMNS:
            for label, key in LEGACY_CATEGORY_LABELS.items():
                connection.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                                   (key, label))

    @contextmanager
    def connect(self):
        """Yield a connection that commits on success, rolls back on error, then closes.

        sqlite3's own context manager only commits or rolls back; it never closes.
        """
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def check(self):
        with self.connect() as connection:
            connection.execute("SELECT COUNT(*) FROM users").fetchone()
            connection.execute("SELECT telegram_photo_file_id, telegram_photo_bot_id, "
                               "photo_version_id FROM products WHERE 1 = 0")
            for table, columns in {**ORDER_TABLE_COLUMNS, **ABUSE_TABLE_COLUMNS}.items():
                connection.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE 1 = 0")

    def get_user(self, telegram_user_id):
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users "
                "WHERE telegram_user_id = ? AND deleted_at IS NULL", (telegram_user_id,)
            ).fetchone()
            if row is None:
                return None
            user = dict(row)
            user["phone_verified"] = bool(user["phone_verified"])
            return user

    def upsert_user(self, telegram_user_id, full_name, email, phone, phone_verified=False,
                    role=None):
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO users (telegram_user_id, full_name, email, phone, phone_verified, role)
                VALUES (?, ?, ?, ?, ?, COALESCE(?, 'CUSTOMER'))
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    full_name=excluded.full_name, email=excluded.email,
                    phone=excluded.phone, phone_verified=excluded.phone_verified,
                    role=COALESCE(?, users.role),
                    deleted_at=NULL
            """, (telegram_user_id, full_name, email, phone, int(phone_verified),
                  role, role))
        return self.get_user(telegram_user_id)

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
                    business_phone=excluded.business_phone,
                    deleted_at=NULL
            """, (telegram_user_id, business_name, business_email,
                  address, business_phone))
            connection.execute(
                "UPDATE users SET role = 'SELLER' WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )

    def get_seller(self, telegram_user_id):
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT {SELLER_COLUMNS} FROM sellers "
                "WHERE telegram_user_id = ? AND deleted_at IS NULL",
                (telegram_user_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_product(self, seller_id, product_id, name, category, description,
                       price_minor, stock_quantity):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order_db = OrderDB(connection)
            try:
                self.guard_stock(order_db, product_id, seller_id, stock_quantity)
            finally:
                order_db.cursor.close()
            cursor = connection.execute("""
                UPDATE products SET name = ?, category = ?, description = ?,
                       price_minor = ?, stock_quantity = ?
                WHERE product_id = ? AND seller_telegram_user_id = ?
                  AND deleted_at IS NULL
            """, (name, category, description, price_minor, stock_quantity,
                  product_id, seller_id))
            return cursor.rowcount == 1

    def set_product_photo(self, seller_id, product_id, object_key, version_id=None):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE products SET photo_object_key = ?, photo_version_id = ?,
                       telegram_photo_file_id = NULL, telegram_photo_bot_id = NULL
                WHERE product_id = ? AND seller_telegram_user_id = ?
                  AND deleted_at IS NULL
                  AND photo_object_key IS NULL
            """, (object_key, version_id, product_id, seller_id))
            if cursor.rowcount != 1:
                raise LookupError("Product not found for this seller")

    def set_stock(self, seller_id, product_id, quantity):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order_db = OrderDB(connection)
            try:
                self.guard_stock(order_db, product_id, seller_id, quantity)
            finally:
                order_db.cursor.close()
            cursor = connection.execute("""
                UPDATE products SET stock_quantity = ?
                WHERE product_id = ? AND seller_telegram_user_id = ?
                  AND deleted_at IS NULL
            """, (quantity, product_id, seller_id))
            return cursor.rowcount == 1

    def get_product(self, product_id):
        with self.connect() as connection:
            row = connection.execute(f"""
                SELECT {PRODUCT_COLUMNS}, p.photo_version_id,
                       p.telegram_photo_file_id, p.telegram_photo_bot_id FROM products p
                JOIN sellers s ON s.telegram_user_id = p.seller_telegram_user_id
                WHERE p.product_id = ? AND p.deleted_at IS NULL
            """, (product_id,)).fetchone()
            return dict(row) if row else None

    def get_seller_product(self, seller_id, seller_number):
        """Resolve a seller's stable listing number, counting erased rows too."""
        if seller_number <= 0:
            return None
        with self.connect() as connection:
            row = connection.execute(f"""
                SELECT {PRODUCT_COLUMNS} FROM products p
                JOIN sellers s ON s.telegram_user_id = p.seller_telegram_user_id
                WHERE p.seller_telegram_user_id = ? AND p.deleted_at IS NULL
                  AND (SELECT COUNT(*) FROM products earlier
                       WHERE earlier.seller_telegram_user_id = p.seller_telegram_user_id
                         AND earlier.product_id <= p.product_id) = ?
            """, (seller_id, seller_number)).fetchone()
            return dict(row) if row else None

    def list_products(self, category=None, seller_id=None):
        return self.list_products_page(category, seller_id)[0]

    def list_products_page(self, category=None, seller_id=None, *, before_id=None):
        """Return (rows, has_more), paging by ID so new inserts cannot shift rows."""
        validate_product_cursor(before_id)
        where = ["p.deleted_at IS NULL"]
        values = []
        if category is not None:
            where.append("p.category = ?")
            values.append(category)
        if seller_id is not None:
            where.append("p.seller_telegram_user_id = ?")
            values.append(seller_id)
        if before_id is not None:
            where.append("p.product_id < ?")
            values.append(before_id)
        with self.connect() as connection:
            rows = connection.execute(f"""
                SELECT {PRODUCT_COLUMNS} FROM products p
                JOIN sellers s ON s.telegram_user_id = p.seller_telegram_user_id
                WHERE {" AND ".join(where)} ORDER BY p.product_id DESC
                LIMIT {PRODUCT_PAGE_SIZE + 1}
            """, values).fetchall()
            return [dict(row) for row in rows[:PRODUCT_PAGE_SIZE]], len(rows) > PRODUCT_PAGE_SIZE

    def count_products(self, seller_id):
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM products "
                "WHERE seller_telegram_user_id = ? AND deleted_at IS NULL", (seller_id,)
            ).fetchone()[0]

    def erase_user(self, telegram_user_id):
        """Overwrite a user's personal data and hide their products, in one transaction.

        Only UPDATEs are used, so the runtime database account needs no DELETE grant.
        The numeric Telegram ID and the erasure time remain. Returns the stored photos
        as (object_key, version_id) pairs so the caller can delete the objects; an
        unknown or already erased user returns an empty list.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""
                UPDATE users SET full_name = ?, email = ?, phone = ?, phone_verified = 0,
                       role = 'CUSTOMER', preferred_category = NULL,
                       deleted_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ? AND deleted_at IS NULL
            """, (ERASED, ERASED, ERASED, telegram_user_id))
            if cursor.rowcount != 1:
                return []
            connection.execute("""
                UPDATE sellers SET business_name = ?, business_email = ?, address = ?,
                       business_phone = ?, deleted_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ? AND deleted_at IS NULL
            """, (ERASED, ERASED, ERASED, ERASED, telegram_user_id))
            order_db = OrderDB(connection)
            try:
                self.erase_orders(order_db, telegram_user_id)
            finally:
                order_db.cursor.close()
            photos = [(row["photo_object_key"], row["photo_version_id"]) for row in
                      connection.execute("""
                          SELECT photo_object_key, photo_version_id FROM products
                          WHERE seller_telegram_user_id = ? AND photo_object_key IS NOT NULL
                      """, (telegram_user_id,))]
            connection.execute("""
                UPDATE products SET name = ?, description = NULL, stock_quantity = 0,
                       photo_object_key = NULL, photo_version_id = NULL,
                       telegram_photo_file_id = NULL, telegram_photo_bot_id = NULL,
                       deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP)
                WHERE seller_telegram_user_id = ?
            """, (ERASED, telegram_user_id))
            return photos


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

    def delete_photo(self, object_key, version_id=None):
        """Remove a stored photo; a photo that is already gone counts as removed."""
        if version_id not in (None, object_key):
            raise ValueError("Invalid local photo version")
        self._path(object_key).unlink(missing_ok=True)
