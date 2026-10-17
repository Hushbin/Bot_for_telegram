"""Seller-panel operations on the bot's existing tables (no second catalog)."""
from catalog import CATEGORIES
from orders import OrderError


class SellerStoreMixin:
    def cache_product_photo(self, pid, object_key, version_id, bot_id, file_id):
        """A late Telegram send may only cache the exact photo it uploaded."""
        with self.order_transaction(True) as db:
            return db.execute(
                f"UPDATE {db.table('products')} SET telegram_photo_file_id = :file_id, "
                "telegram_photo_bot_id = :bot_id WHERE product_id = :pid "
                "AND deleted_at IS NULL AND photo_object_key = :object_key "
                "AND (photo_version_id = :version_id OR "
                "(photo_version_id IS NULL AND :version_id IS NULL))",
                pid=pid, object_key=object_key, version_id=version_id,
                bot_id=bot_id, file_id=file_id) == 1

    def clear_product_photo_cache(self, pid, object_key, version_id, bot_id, file_id):
        """Do not clear a newer reference or a concurrently replaced photo."""
        with self.order_transaction(True) as db:
            return db.execute(
                f"UPDATE {db.table('products')} SET telegram_photo_file_id = NULL, "
                "telegram_photo_bot_id = NULL WHERE product_id = :pid "
                "AND deleted_at IS NULL AND photo_object_key = :object_key "
                "AND (photo_version_id = :version_id OR "
                "(photo_version_id IS NULL AND :version_id IS NULL)) "
                "AND telegram_photo_bot_id = :bot_id AND telegram_photo_file_id = :file_id",
                pid=pid, object_key=object_key, version_id=version_id,
                bot_id=bot_id, file_id=file_id) == 1

    def replace_seller_product_photo(self, uid, pid, object_key, version_id, expected_key):
        """Attach an uploaded photo only if the live account/product still matches."""
        with self.order_transaction(True) as db:
            db.accounts(uid)
            if not self._live(db, 'users', uid) or not self._live(db, 'sellers', uid):
                raise OrderError('permission', 'Register as a seller in the bot first.')
            product = self._product(db, pid, lock=True)
            if not product or product['seller_telegram_user_id'] != uid or product['deleted_at']:
                raise OrderError('missing', 'Product not found.')
            if product['photo_object_key'] != expected_key:
                raise OrderError('conflict', 'The photo changed. Reopen the product before uploading again.')
            db.execute(f"UPDATE {db.table('products')} SET photo_object_key = :object_key, "
                       'photo_version_id = :version_id, telegram_photo_file_id = NULL, '
                       'telegram_photo_bot_id = NULL WHERE product_id = :pid',
                       object_key=object_key, version_id=version_id, pid=pid)
            return product['photo_object_key'], product['photo_version_id']

    def seller_snapshot(self, uid):
        with self.order_transaction() as db:
            user = self._live(db, 'users', uid)
            seller = self._live(db, 'sellers', uid)
            if not user or not seller:
                raise OrderError('permission', 'Register as a seller in the bot first.')
            products = db.all(f"SELECT * FROM {db.table('products')} "
                              'WHERE seller_telegram_user_id = :account_id AND deleted_at IS NULL '
                              'ORDER BY product_id', account_id=uid)
            orders = db.all(f"SELECT * FROM {db.table('orders')} "
                            'WHERE seller_id = :account_id AND seller_erased_at IS NULL '
                            'ORDER BY order_id DESC', account_id=uid)
            return dict(user=user, seller=seller, products=products, orders=orders)

    def save_seller_product(self, uid, values, pid=None):
        name = values.get('name', '')
        description = values.get('description', '')
        if (not isinstance(name, str) or not name.strip() or len(name) > 160
                or not isinstance(description, str) or len(description) > 1000
                or values.get('category') not in CATEGORIES
                or type(values.get('price_minor')) is not int
                or not 0 <= values['price_minor'] < 10**12
                or type(values.get('stock_quantity')) is not int
                or not 0 <= values['stock_quantity'] < 10**10):
            raise OrderError('validation', 'Check the product name, category, price and stock.')
        with self.order_transaction(True) as db:
            db.accounts(uid)
            if not self._live(db, 'users', uid) or not self._live(db, 'sellers', uid):
                raise OrderError('permission', 'Register as a seller in the bot first.')
            data = {key: values[key] for key in
                    ('name', 'description', 'category', 'price_minor', 'stock_quantity')}
            data['name'] = name.strip()
            if pid is None:
                return db.insert('products', dict(data, seller_telegram_user_id=uid), 'product_id')
            product = self._product(db, pid, lock=True)
            if not product or product['seller_telegram_user_id'] != uid or product['deleted_at']:
                raise OrderError('missing', 'Product not found.')
            # Do not overwrite stock reserved by an order since the editor was opened.
            if values.get('expected_stock') != product['stock_quantity']:
                raise OrderError('conflict', 'Stock changed. Refresh and review the product before saving.')
            self.guard_stock(db, pid, uid, data['stock_quantity'])
            assignments = ', '.join(f'{key} = :{key}' for key in data)
            db.execute(f"UPDATE {db.table('products')} SET {assignments} WHERE product_id = :pid",
                       **data, pid=pid)
            return pid

    def remove_seller_product(self, uid, pid):
        with self.order_transaction(True) as db:
            db.accounts(uid)
            if not self._live(db, 'users', uid) or not self._live(db, 'sellers', uid):
                raise OrderError('permission', 'Register as a seller in the bot first.')
            product = self._product(db, pid, lock=True)
            if not product or product['seller_telegram_user_id'] != uid or product['deleted_at']:
                raise OrderError('missing', 'Product not found.')
            accepted = db.one(f"SELECT COUNT(*) AS count FROM {db.table('orders')} "
                              "WHERE product_id = :pid AND status = 'ACCEPTED'", pid=pid)
            if accepted['count']:
                raise OrderError('conflict', 'Complete or cancel accepted orders before removing this product.')
            db.execute(f"UPDATE {db.table('products')} SET deleted_at = :now, "
                       "telegram_photo_file_id = NULL, telegram_photo_bot_id = NULL WHERE product_id = :pid",
                       now=self._order_now(db), pid=pid)

    def save_seller_profile(self, uid, values):
        limits = dict(business_name=160, business_email=254, address=400, business_phone=40)
        data = {}
        for key, limit in limits.items():
            value = values.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise OrderError('validation', 'Complete all store contact fields within their limits.')
            data[key] = value.strip()
        if '@' not in data['business_email']:
            raise OrderError('validation', 'Enter a valid contact email.')
        with self.order_transaction(True) as db:
            db.accounts(uid)
            if not self._live(db, 'users', uid) or not self._live(db, 'sellers', uid):
                raise OrderError('permission', 'Register as a seller in the bot first.')
            assignments = ', '.join(f'{key} = :{key}' for key in data)
            db.execute(f"UPDATE {db.table('sellers')} SET {assignments} WHERE telegram_user_id = :account_id",
                       **data, account_id=uid)
