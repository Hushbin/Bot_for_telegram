"""Small Oracle adapter for the bot's current data contract."""

import oracledb


SCHEMA = "SME_SCHEMA"


def row_dict(cursor, row):
    if row is None:
        return None
    return {column[0].lower(): value for column, value in zip(cursor.description, row)}


class OracleStore:
    def __init__(self, settings):
        self.connection_args = dict(
            user=settings.db_user,
            password=settings.db_password,
            dsn=settings.db_dsn,
            config_dir=settings.db_wallet_dir,
            wallet_location=settings.db_wallet_dir,
            wallet_password=settings.db_wallet_password,
        )

    def connect(self):
        return oracledb.connect(**self.connection_args)

    def check(self):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.USERS")
                cursor.fetchone()

    def upsert_user(self, telegram_user_id, full_name, email, phone):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    MERGE INTO {SCHEMA}.USERS u
                    USING (SELECT :telegram_user_id AS telegram_user_id FROM dual) incoming
                    ON (u.telegram_user_id = incoming.telegram_user_id)
                    WHEN MATCHED THEN UPDATE SET
                        full_name = :full_name, email = :email, phone = :phone,
                        updated_at = SYSTIMESTAMP
                    WHEN NOT MATCHED THEN INSERT
                        (telegram_user_id, full_name, email, phone)
                        VALUES (:telegram_user_id_insert, :full_name_insert,
                                :email_insert, :phone_insert)
                """, telegram_user_id=telegram_user_id, full_name=full_name,
                    email=email, phone=phone, telegram_user_id_insert=telegram_user_id,
                    full_name_insert=full_name, email_insert=email, phone_insert=phone)
            connection.commit()
        return self.get_user(telegram_user_id)

    def get_user(self, telegram_user_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT telegram_user_id, full_name, email, phone, role,
                           preferred_category
                    FROM {SCHEMA}.USERS WHERE telegram_user_id = :id
                """, id=telegram_user_id)
                return row_dict(cursor, cursor.fetchone())

    def set_role(self, telegram_user_id, role):
        if role not in ("CUSTOMER", "SELLER"):
            raise ValueError("Invalid role")
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {SCHEMA}.USERS SET role = :role,
                           updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id
                """, role=role, id=telegram_user_id)
                if cursor.rowcount != 1:
                    raise LookupError("User not found")
            connection.commit()

    def set_preference(self, telegram_user_id, category):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {SCHEMA}.USERS SET preferred_category = :category,
                           updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id
                """, category=category, id=telegram_user_id)
            connection.commit()

    def upsert_seller(self, telegram_user_id, business_name, business_email,
                      address, business_phone):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    MERGE INTO {SCHEMA}.SELLERS s
                    USING (SELECT :id AS telegram_user_id FROM dual) incoming
                    ON (s.telegram_user_id = incoming.telegram_user_id)
                    WHEN MATCHED THEN UPDATE SET
                        business_name = :name, business_email = :email,
                        address = :address, business_phone = :phone,
                        updated_at = SYSTIMESTAMP
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
                    UPDATE {SCHEMA}.USERS SET role = 'SELLER', updated_at = SYSTIMESTAMP
                    WHERE telegram_user_id = :id
                """, id=telegram_user_id)
            connection.commit()

    def get_seller(self, telegram_user_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT telegram_user_id, business_name, business_email,
                           address, business_phone
                    FROM {SCHEMA}.SELLERS WHERE telegram_user_id = :id
                """, id=telegram_user_id)
                return row_dict(cursor, cursor.fetchone())

    def create_product(self, seller_id, name, category, description,
                       price_kobo, stock_quantity):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                new_id = cursor.var(oracledb.DB_TYPE_NUMBER)
                cursor.execute(f"""
                    INSERT INTO {SCHEMA}.PRODUCTS
                        (seller_telegram_user_id, name, category, description,
                         price_kobo, stock_quantity)
                    VALUES (:seller_id, :name, :category, :description,
                            :price_kobo, :stock_quantity)
                    RETURNING product_id INTO :new_id
                """, seller_id=seller_id, name=name, category=category,
                    description=description, price_kobo=price_kobo,
                    stock_quantity=stock_quantity, new_id=new_id)
                product_id = int(new_id.getvalue()[0])
            connection.commit()
        return product_id

    def set_product_photo(self, seller_id, product_id, object_key):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {SCHEMA}.PRODUCTS SET photo_object_key = :object_key,
                           updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                """, object_key=object_key, product_id=product_id,
                    seller_id=seller_id)
                if cursor.rowcount != 1:
                    raise LookupError("Product not found for this seller")
            connection.commit()

    def set_stock(self, seller_id, product_id, quantity):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    UPDATE {SCHEMA}.PRODUCTS SET stock_quantity = :quantity,
                           updated_at = SYSTIMESTAMP
                    WHERE product_id = :product_id
                      AND seller_telegram_user_id = :seller_id
                """, quantity=quantity, product_id=product_id, seller_id=seller_id)
                changed = cursor.rowcount == 1
            connection.commit()
        return changed

    def get_product(self, product_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id, p.name,
                           p.category, p.description, p.price_kobo,
                           p.stock_quantity, p.photo_object_key, s.business_name
                    FROM {SCHEMA}.PRODUCTS p JOIN {SCHEMA}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    WHERE p.product_id = :id
                """, id=product_id)
                return row_dict(cursor, cursor.fetchone())

    def list_products(self, category=None, seller_id=None):
        where = []
        binds = {}
        if category is not None:
            where.append("p.category = :category")
            binds["category"] = category
        if seller_id is not None:
            where.append("p.seller_telegram_user_id = :seller_id")
            binds["seller_id"] = seller_id
        condition = "WHERE " + " AND ".join(where) if where else ""
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT p.product_id, p.seller_telegram_user_id, p.name,
                           p.category, p.description, p.price_kobo,
                           p.stock_quantity, p.photo_object_key, s.business_name
                    FROM {SCHEMA}.PRODUCTS p JOIN {SCHEMA}.SELLERS s
                      ON p.seller_telegram_user_id = s.telegram_user_id
                    {condition}
                    ORDER BY p.product_id DESC FETCH FIRST 20 ROWS ONLY
                """, binds)
                return [row_dict(cursor, row) for row in cursor.fetchall()]
