"""In-memory stand-ins for the store and media adapters.

FakeStore mirrors the real stores' behaviour (soft erase, ownership checks, foreign
key on sellers) and is held to the same contract by tests/test_store_contract.py.
"""

ERASED = "[deleted]"
USER_KEYS = ("telegram_user_id", "full_name", "email", "phone", "phone_verified",
             "role", "preferred_category")
SELLER_KEYS = ("telegram_user_id", "business_name", "business_email", "address",
               "business_phone")


class FakeStore:
    def __init__(self):
        self.users = {}
        self.sellers = {}
        self.products = {}
        self.fail_photo_update = False

    def check(self):
        pass

    def get_user(self, user_id):
        user = self.users.get(user_id)
        if user is None or user["deleted"]:
            return None
        return {key: user[key] for key in USER_KEYS}

    def upsert_user(self, user_id, name, email, phone, phone_verified=False, role=None):
        user = self.users.setdefault(user_id, {"role": "CUSTOMER", "preferred_category": None})
        user.update(telegram_user_id=user_id, full_name=name, email=email, phone=phone,
                    phone_verified=bool(phone_verified), deleted=False)
        if role is not None:
            user["role"] = role
        return self.get_user(user_id)

    def set_preference(self, user_id, category):
        self.users[user_id]["preferred_category"] = category

    def upsert_seller(self, user_id, business_name, business_email, address, business_phone):
        if user_id not in self.users:
            raise LookupError("A seller needs a registered user")
        self.sellers[user_id] = dict(
            telegram_user_id=user_id, business_name=business_name,
            business_email=business_email, address=address,
            business_phone=business_phone, deleted=False)
        self.users[user_id]["role"] = "SELLER"

    def get_seller(self, user_id):
        seller = self.sellers.get(user_id)
        if seller is None or seller["deleted"]:
            return None
        return {key: seller[key] for key in SELLER_KEYS}

    def create_product(self, user_id, name, category, description, price_kobo, stock,
                       with_number=False):
        if user_id not in self.sellers:
            raise LookupError("Unknown seller")
        product_id = len(self.products) + 1
        self.products[product_id] = dict(
            product_id=product_id, seller_telegram_user_id=user_id, name=name,
            category=category, description=description, price_kobo=price_kobo,
            stock_quantity=stock, photo_object_key=None, photo_version_id=None,
            deleted=False)
        seller_number = sum(1 for product in self.products.values()
                            if product["seller_telegram_user_id"] == user_id
                            and product["product_id"] <= product_id)
        return (product_id, seller_number) if with_number else product_id

    def _live(self, product_id, seller_id=None):
        product = self.products.get(product_id)
        if product is None or product["deleted"]:
            return None
        if seller_id is not None and product["seller_telegram_user_id"] != seller_id:
            return None
        return product

    def _view(self, product):
        view = {key: product[key] for key in (
            "product_id", "seller_telegram_user_id", "name", "category", "description",
            "price_kobo", "stock_quantity", "photo_object_key")}
        view["business_name"] = self.sellers[product["seller_telegram_user_id"]]["business_name"]
        view["seller_number"] = sum(
            1 for earlier in self.products.values()
            if earlier["seller_telegram_user_id"] == product["seller_telegram_user_id"]
            and earlier["product_id"] <= product["product_id"]
        )
        return view

    def get_product(self, product_id):
        product = self._live(product_id)
        return self._view(product) if product else None

    def get_seller_product(self, seller_id, seller_number):
        if seller_number <= 0:
            return None
        products = sorted((p for p in self.products.values()
                           if p["seller_telegram_user_id"] == seller_id),
                          key=lambda p: p["product_id"])
        if seller_number > len(products):
            return None
        product = products[seller_number - 1]
        return self._view(product) if not product["deleted"] else None

    def update_product(self, user_id, product_id, name, category, description,
                       price_kobo, stock):
        product = self._live(product_id, user_id)
        if product is None:
            return False
        product.update(name=name, category=category, description=description,
                       price_kobo=price_kobo, stock_quantity=stock)
        return True

    def set_product_photo(self, user_id, product_id, key, version_id=None):
        if self.fail_photo_update:
            raise RuntimeError("Database unavailable")
        product = self._live(product_id, user_id)
        if product is None:
            raise LookupError("Product not found for this seller")
        product.update(photo_object_key=key, photo_version_id=version_id)

    def set_stock(self, user_id, product_id, stock):
        product = self._live(product_id, user_id)
        if product is None:
            return False
        product["stock_quantity"] = stock
        return True

    def list_products(self, category=None, seller_id=None):
        rows = [p for p in self.products.values() if not p["deleted"]
                and (category is None or p["category"] == category)
                and (seller_id is None or p["seller_telegram_user_id"] == seller_id)]
        rows.sort(key=lambda p: p["product_id"], reverse=True)
        return [self._view(p) for p in rows[:20]]

    def count_products(self, seller_id):
        return sum(1 for p in self.products.values()
                   if p["seller_telegram_user_id"] == seller_id and not p["deleted"])

    def erase_user(self, user_id):
        user = self.users.get(user_id)
        if user is None or user["deleted"]:
            return []
        user.update(full_name=ERASED, email=ERASED, phone=ERASED, phone_verified=False,
                    role="CUSTOMER", preferred_category=None, deleted=True)
        seller = self.sellers.get(user_id)
        if seller and not seller["deleted"]:
            seller.update(business_name=ERASED, business_email=ERASED, address=ERASED,
                          business_phone=ERASED, deleted=True)
        photos = []
        for product in self.products.values():
            if product["seller_telegram_user_id"] != user_id:
                continue
            if product["photo_object_key"]:
                photos.append((product["photo_object_key"], product["photo_version_id"]))
            product.update(name=ERASED, description=None, stock_quantity=0,
                           photo_object_key=None, photo_version_id=None, deleted=True)
        return photos


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
