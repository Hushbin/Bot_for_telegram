"""Small Oracle adapter for the bot's current data contract."""

import json
import logging
from time import monotonic_ns

import oracledb

from catalog import PRODUCT_PAGE_SIZE, validate_product_cursor
from order_store import OrderDB, OrderStoreMixin
from seller_store import SellerStoreMixin
from abuse_schema import TABLE_COLUMNS as ABUSE_TABLE_COLUMNS
from order_schema import TABLE_COLUMNS as ORDER_TABLE_COLUMNS


SCHEMA = "SME_SCHEMA"
ERASED = "[deleted]"
LOG = logging.getLogger(__name__)
# Columns added after the first release; keep in step with scripts/setup_db.py.
REQUIRED_COLUMNS = {
    "USERS": ("telegram_user_id", "phone_verified", "deleted_at"),
    "SELLERS": ("telegram_user_id", "deleted_at"),
    "PRODUCTS": ("product_id", "deleted_at", "photo_version_id", "price_minor", "currency",
                 "telegram_photo_file_id", "telegram_photo_bot_id"),
    **ORDER_TABLE_COLUMNS,
    **ABUSE_TABLE_COLUMNS,
}


def row_dict(cursor, row):
    if row is None:
        return None
    return {column[0].lower(): value for column, value in zip(cursor.description, row)}


def _record_connection_open(started, outcome, error_type=None):
    """Best-effort diagnostics must never change a connection's ownership or outcome."""
    if started is None:
        return
    try:
        event = dict(v=1, elapsed_ms=(monotonic_ns() - started) / 1_000_000, outcome=outcome)
        if error_type is not None:
            event["error_type"] = error_type
        LOG.info("DB_CONNECTION %s", json.dumps(event, separators=(",", ":"), allow_nan=False))
    except Exception:
        # In particular, a failing logging handler must not lose an opened connection
        # or replace the exception raised by the driver. Never log secret-bearing args.
        pass


class OracleStore(SellerStoreMixin, OrderStoreMixin):
    def __init__(self, settings):
        from abuse_limits import AbuseLimits
        self.abuse_limits = getattr(settings, 'abuse_limits', AbuseLimits())
        self.schema = getattr(settings, "db_schema", None) or SCHEMA
        self.connection_args = dict(
            user=settings.db_user,
            password=settings.db_password,
            dsn=settings.db_dsn,
        )
        if settings.db_wallet_dir:  # mTLS Autonomous Database; a plain database has none
            self.connection_args.update(
                config_dir=settings.db_wallet_dir,
                wallet_location=settings.db_wallet_dir,
                wallet_password=settings.db_wallet_password,
            )

    def connect(self):
        try:
            started = monotonic_ns()
        except Exception:
            started = None
        try:
            connection = oracledb.connect(**self.connection_args)
        except Exception as exc:
            _record_connection_open(started, "failure", type(exc).__name__)
            raise
        _record_connection_open(started, "success")
        return connection

    def check(self):
        """Fail at startup, with a clear message, if the schema predates this release.

        Deploys roll back automatically when the service does not stay up, so a bot
        started before scripts/setup_db.py has migrated the database never goes live.
        """
        try:
            with self.connect() as connection:
                with connection.cursor() as cursor:
                    for table, columns in REQUIRED_COLUMNS.items():
                        cursor.execute(
                            f"SELECT {', '.join(columns)} FROM {self.schema}.{table} WHERE 1 = 0"
                        )
        except oracledb.DatabaseError as exc:
            raise RuntimeError(
                "Database schema is missing columns this version needs; "
                "run scripts/setup_db.py before starting the bot."
            ) from exc

    def upsert_user(self, telegram_user_id, full_name, email, phone, phone_verified=False,
                    role=None):
        verified = int(phone_verified)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    MERGE INTO {self.schema}.USERS u
                    USING (SELECT :telegram_user_id AS telegram_user_id FROM dual) incoming
                    ON (u.telegram_user_id = incoming.telegram_user_id)
                    WHEN MATCHED THEN UPDATE SET
                        full_name = :full_name, email = :email, phone = :phone,
                        phone_verified = :phone_verified,
                        role = COALESCE(:role, u.role), deleted_at = NULL,
                        updated_at = SYSTIMESTAMP
                    WHEN NOT MATCHED THEN INSERT
                        (telegram_user_id, full_name, email, phone, phone_verified, role)
                        VALUES (:telegram_user_id_insert, :full_name_insert,
                                :email_insert, :phone_insert, :phone_verified_insert,
                                COALESCE(:role_insert, 'CUSTOMER'))
                """, telegram_user_id=telegram_user_id, full_name=full_name,
                    email=email, phone=phone, phone_verified=verified,
                    role=role, role_insert=role,
                    telegram_user_id_insert=telegram_user_id,
                    full_name_insert=full_name, email_insert=email, phone_insert=phone,
                    phone_verified_insert=verified)
            connection.commit()
        return self.get_user(telegram_user_id)

    def get_user(self, telegram_user_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT telegram_user_id, full_name, email, phone, phone_verified,
                           role, preferred_category
                    FROM {self.schema}.USERS
                    WHERE telegram_user_id = :id AND deleted_at IS NULL
                """, id=telegram_user_id)
                user = row_dict(cursor, cursor.fetchone())
        if user is not None:
            user["phone_verified"] = bool(user["phone_verified"])
        return user

    def set_preference(self, telegram_user_id, category):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.USERS SET preferred_category = :category,
                           updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id
                """, category=category, id=telegram_user_id)
            connection.commit()

    def upsert_seller(self, telegram_user_id, business_name, business_email,
                      address, business_phone):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT telegram_user_id FROM {self.schema}.USERS "
                               "WHERE telegram_user_id = :id FOR UPDATE", id=telegram_user_id)
                cursor.fetchone()
                cursor.execute(f"""
                    MERGE INTO {self.schema}.SELLERS s
                    USING (SELECT :id AS telegram_user_id FROM dual) incoming
                    ON (s.telegram_user_id = incoming.telegram_user_id)
                    WHEN MATCHED THEN UPDATE SET
                        business_name = :name, business_email = :email,
                        address = :address, business_phone = :phone,
                        deleted_at = NULL, updated_at = SYSTIMESTAMP
                    WHEN NOT MATCHED THEN INSERT
                        (telegram_user_id, business_name, business_email, address,
                         business_phone)
                        VALUES (:id_insert, :name_insert, :email_insert,
                                :address_insert, :phone_insert)
                """, id=telegram_user_id, name=business_name, email=business_email,
                    address=address, phone=business_phone, id_insert=telegram_user_id,
                    name_insert=business_name, email_insert=business_email,
                    address_insert=address, phone_insert=business_phone)
                cursor.execute(f"""
                    UPDATE {self.schema}.USERS SET role = 'SELLER', updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id
                """, id=telegram_user_id)
            connection.commit()

    def get_seller(self, telegram_user_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT telegram_user_id, business_name, business_email,
                           address, business_phone
                    FROM {self.schema}.SELLERS
                    WHERE telegram_user_id = :id AND deleted_at IS NULL
                """, id=telegram_user_id)
                return row_dict(cursor, cursor.fetchone())

    def update_product(self, seller_id, product_id, name, category, description,
                       price_minor, stock_quantity):
        with self.connect() as connection:
            order_db = OrderDB(connection, self.schema)
            try:
                self.guard_stock(order_db, product_id, seller_id, stock_quantity)
            finally:
                order_db.cursor.close()
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET name = :name, category = :category,
                           description = :description, price_minor = :price_minor,
                           stock_quantity = :stock_quantity, updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                      AND deleted_at IS NULL
                """, name=name, category=category, description=description,
                    price_minor=price_minor, stock_quantity=stock_quantity,
                    product_id=product_id, seller_id=seller_id)
                changed = cursor.rowcount == 1
            connection.commit()
        return changed

    def set_product_photo(self, seller_id, product_id, object_key, version_id=None):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET photo_object_key = :object_key,
                           photo_version_id = :version_id, telegram_photo_file_id = NULL,
                           telegram_photo_bot_id = NULL, updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                      AND deleted_at IS NULL
                      AND photo_object_key IS NULL
                """, object_key=object_key, version_id=version_id,
                    product_id=product_id, seller_id=seller_id)
                if cursor.rowcount != 1:
                    raise LookupError("Product not found for this seller")
            connection.commit()

    def set_stock(self, seller_id, product_id, quantity):
        with self.connect() as connection:
            order_db = OrderDB(connection, self.schema)
            try:
                self.guard_stock(order_db, product_id, seller_id, quantity)
            finally:
                order_db.cursor.close()
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET stock_quantity = :quantity,
                           updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                      AND deleted_at IS NULL
                """, quantity=quantity, product_id=product_id, seller_id=seller_id)
                changed = cursor.rowcount == 1
            connection.commit()
        return changed

    def get_product(self, product_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id,
                           (SELECT COUNT(*) FROM {self.schema}.PRODUCTS earlier
                            WHERE earlier.seller_telegram_user_id = p.seller_telegram_user_id
                              AND earlier.product_id <= p.product_id) AS seller_number,
                           p.name,
                           p.category, p.description, p.price_minor, p.currency,
                           p.stock_quantity, p.photo_object_key, p.photo_version_id,
                           p.telegram_photo_file_id, p.telegram_photo_bot_id, s.business_name
                    FROM {self.schema}.PRODUCTS p JOIN {self.schema}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    WHERE p.product_id = :id AND p.deleted_at IS NULL
                """, id=product_id)
                return row_dict(cursor, cursor.fetchone())

    def list_products(self, category=None, seller_id=None):
        return self.list_products_page(category, seller_id)[0]

    def list_products_page(self, category=None, seller_id=None, *, before_id=None):
        """Return (rows, has_more), paging by ID so new inserts cannot shift rows."""
        validate_product_cursor(before_id)
        where = ["p.deleted_at IS NULL"]
        binds = {}
        if category is not None:
            where.append("p.category = :category")
            binds["category"] = category
        if seller_id is not None:
            where.append("p.seller_telegram_user_id = :seller_id")
            binds["seller_id"] = seller_id
        if before_id is not None:
            where.append("p.product_id < :before_id")
            binds["before_id"] = before_id
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id,
                           (SELECT COUNT(*) FROM {self.schema}.PRODUCTS earlier
                            WHERE earlier.seller_telegram_user_id = p.seller_telegram_user_id
                              AND earlier.product_id <= p.product_id) AS seller_number,
                           p.name,
                           p.category, p.description, p.price_minor, p.currency,
                           p.stock_quantity, p.photo_object_key, s.business_name
                    FROM {self.schema}.PRODUCTS p JOIN {self.schema}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    WHERE {" AND ".join(where)}
                    ORDER BY p.product_id DESC FETCH FIRST {PRODUCT_PAGE_SIZE + 1} ROWS ONLY
                """, binds)
                rows = cursor.fetchall()
                return ([row_dict(cursor, row) for row in rows[:PRODUCT_PAGE_SIZE]],
                        len(rows) > PRODUCT_PAGE_SIZE)

    def get_seller_product(self, seller_id, seller_number):
        """Resolve a seller's stable listing number, counting erased rows too."""
        if seller_number <= 0:
            return None
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id,
                           :seller_number AS seller_number, p.name, p.category,
                           p.description, p.price_minor, p.currency, p.stock_quantity,
                           p.photo_object_key, s.business_name
                    FROM {self.schema}.PRODUCTS p JOIN {self.schema}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    WHERE p.seller_telegram_user_id = :seller_id
                      AND p.deleted_at IS NULL
                      AND (SELECT COUNT(*) FROM {self.schema}.PRODUCTS earlier
                           WHERE earlier.seller_telegram_user_id = p.seller_telegram_user_id
                             AND earlier.product_id <= p.product_id) = :number_match
                """, seller_id=seller_id, seller_number=seller_number,
                    number_match=seller_number)
                return row_dict(cursor, cursor.fetchone())

    def count_products(self, seller_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT COUNT(*) FROM {self.schema}.PRODUCTS
                    WHERE seller_telegram_user_id = :seller_id AND deleted_at IS NULL
                """, seller_id=seller_id)
                return int(cursor.fetchone()[0])

    def erase_user(self, telegram_user_id):
        """Overwrite a user's personal data and hide their products, in one transaction.

        Only UPDATEs are used, so the runtime database account needs no DELETE grant.
        The numeric Telegram ID and the erasure time remain. Returns the stored photos
        as (object_key, version_id) pairs so the caller can delete the objects; an
        unknown or already erased user returns an empty list.
        """
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.USERS SET full_name = :erased_name,
                           email = :erased_email, phone = :erased_phone,
                           phone_verified = 0, role = 'CUSTOMER',
                           preferred_category = NULL, deleted_at = SYSTIMESTAMP,
                           updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id AND deleted_at IS NULL
                """, erased_name=ERASED, erased_email=ERASED, erased_phone=ERASED,
                    id=telegram_user_id)
                if cursor.rowcount != 1:
                    connection.rollback()
                    return []
                cursor.execute(f"""
                    UPDATE {self.schema}.SELLERS SET business_name = :erased_name,
                           business_email = :erased_email, address = :erased_address,
                           business_phone = :erased_phone, deleted_at = SYSTIMESTAMP,
                           updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id AND deleted_at IS NULL
                """, erased_name=ERASED, erased_email=ERASED, erased_address=ERASED,
                    erased_phone=ERASED, id=telegram_user_id)
                order_db = OrderDB(connection, self.schema)
                try:
                    self.erase_orders(order_db, telegram_user_id)
                finally:
                    order_db.cursor.close()
                cursor.execute(f"""
                    SELECT photo_object_key, photo_version_id FROM {self.schema}.PRODUCTS
                    WHERE seller_telegram_user_id = :id AND photo_object_key IS NOT NULL
                """, id=telegram_user_id)
                photos = [(row[0], row[1]) for row in cursor.fetchall()]
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET name = :erased, description = NULL,
                           stock_quantity = 0, photo_object_key = NULL,
                           photo_version_id = NULL, telegram_photo_file_id = NULL,
                           telegram_photo_bot_id = NULL,
                           deleted_at = COALESCE(deleted_at, SYSTIMESTAMP),
                           updated_at = SYSTIMESTAMP
                    WHERE seller_telegram_user_id = :id
                """, erased=ERASED, id=telegram_user_id)
            connection.commit()
        return photos
