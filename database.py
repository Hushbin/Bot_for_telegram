"""Small Oracle adapter for the bot's current data contract."""

import oracledb


SCHEMA = "SME_SCHEMA"
ERASED = "[deleted]"
# Columns added after the first release; keep in step with scripts/setup_db.py.
REQUIRED_COLUMNS = {
    "USERS": ("telegram_user_id", "phone_verified", "deleted_at"),
    "SELLERS": ("telegram_user_id", "deleted_at"),
    "PRODUCTS": ("product_id", "deleted_at", "photo_version_id"),
}


def row_dict(cursor, row):
    if row is None:
        return None
    return {column[0].lower(): value for column, value in zip(cursor.description, row)}


class OracleStore:
    def __init__(self, settings):
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
        return oracledb.connect(**self.connection_args)

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

    def create_product(self, seller_id, name, category, description,
                       price_kobo, stock_quantity, with_number=False):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                new_id = cursor.var(oracledb.DB_TYPE_NUMBER)
                cursor.execute(f"""
                    INSERT INTO {self.schema}.PRODUCTS
                        (seller_telegram_user_id, name, category, description,
                         price_kobo, stock_quantity)
                    VALUES (:seller_id, :name, :category, :description,
                            :price_kobo, :stock_quantity)
                    RETURNING product_id INTO :new_id
                """, seller_id=seller_id, name=name, category=category,
                    description=description, price_kobo=price_kobo,
                    stock_quantity=stock_quantity, new_id=new_id)
                product_id = int(new_id.getvalue()[0])
                if with_number:
                    cursor.execute(f"""
                        SELECT COUNT(*) FROM {self.schema}.PRODUCTS
                        WHERE seller_telegram_user_id = :seller_id
                          AND product_id <= :product_id
                    """, seller_id=seller_id, product_id=product_id)
                    seller_number = int(cursor.fetchone()[0])
            connection.commit()
        return (product_id, seller_number) if with_number else product_id

    def update_product(self, seller_id, product_id, name, category, description,
                       price_kobo, stock_quantity):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET name = :name, category = :category,
                           description = :description, price_kobo = :price_kobo,
                           stock_quantity = :stock_quantity, updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                      AND deleted_at IS NULL
                """, name=name, category=category, description=description,
                    price_kobo=price_kobo, stock_quantity=stock_quantity,
                    product_id=product_id, seller_id=seller_id)
                changed = cursor.rowcount == 1
            connection.commit()
        return changed

    def set_product_photo(self, seller_id, product_id, object_key, version_id=None):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET photo_object_key = :object_key,
                           photo_version_id = :version_id, updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                      AND deleted_at IS NULL
                """, object_key=object_key, version_id=version_id,
                    product_id=product_id, seller_id=seller_id)
                if cursor.rowcount != 1:
                    raise LookupError("Product not found for this seller")
            connection.commit()

    def set_stock(self, seller_id, product_id, quantity):
        with self.connect() as connection:
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
                           p.category, p.description, p.price_kobo,
                           p.stock_quantity, p.photo_object_key, s.business_name
                    FROM {self.schema}.PRODUCTS p JOIN {self.schema}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    WHERE p.product_id = :id AND p.deleted_at IS NULL
                """, id=product_id)
                return row_dict(cursor, cursor.fetchone())

    def list_products(self, category=None, seller_id=None):
        where = ["p.deleted_at IS NULL"]
        binds = {}
        if category is not None:
            where.append("p.category = :category")
            binds["category"] = category
        if seller_id is not None:
            where.append("p.seller_telegram_user_id = :seller_id")
            binds["seller_id"] = seller_id
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id,
                           (SELECT COUNT(*) FROM {self.schema}.PRODUCTS earlier
                            WHERE earlier.seller_telegram_user_id = p.seller_telegram_user_id
                              AND earlier.product_id <= p.product_id) AS seller_number,
                           p.name,
                           p.category, p.description, p.price_kobo,
                           p.stock_quantity, p.photo_object_key, s.business_name
                    FROM {self.schema}.PRODUCTS p JOIN {self.schema}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    WHERE {" AND ".join(where)}
                    ORDER BY p.product_id DESC FETCH FIRST 20 ROWS ONLY
                """, binds)
                return [row_dict(cursor, row) for row in cursor.fetchall()]

    def get_seller_product(self, seller_id, seller_number):
        """Resolve a seller's stable listing number, counting erased rows too."""
        if seller_number <= 0:
            return None
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id,
                           :seller_number AS seller_number, p.name, p.category,
                           p.description, p.price_kobo, p.stock_quantity,
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
                cursor.execute(f"""
                    SELECT photo_object_key, photo_version_id FROM {self.schema}.PRODUCTS
                    WHERE seller_telegram_user_id = :id AND photo_object_key IS NOT NULL
                """, id=telegram_user_id)
                photos = [(row[0], row[1]) for row in cursor.fetchall()]
                cursor.execute(f"""
                    UPDATE {self.schema}.PRODUCTS SET name = :erased, description = NULL,
                           stock_quantity = 0, photo_object_key = NULL,
                           photo_version_id = NULL,
                           deleted_at = COALESCE(deleted_at, SYSTIMESTAMP),
                           updated_at = SYSTIMESTAMP
                    WHERE seller_telegram_user_id = :id
                """, erased=ERASED, id=telegram_user_id)
            connection.commit()
        return photos
