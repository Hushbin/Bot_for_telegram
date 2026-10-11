import io
import unittest
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from handlers import (
    BotHandlers, CHOOSING, CLASS_STATE, SME_DETAILS, PRODUCT_DETAILS,
    PRODUCT_PHOTO, parse_product,
)


class FakeStore:
    def __init__(self):
        self.users = {}
        self.sellers = {}
        self.products = {}
        self.fail_photo_update = False

    def get_user(self, user_id):
        return self.users.get(user_id)

    def upsert_user(self, user_id, name, email, phone):
        user = self.users.setdefault(user_id, {"role": "CUSTOMER", "preferred_category": None})
        user.update(telegram_user_id=user_id, full_name=name, email=email, phone=phone)
        return user.copy()

    def set_role(self, user_id, role):
        self.users[user_id]["role"] = role

    def set_preference(self, user_id, category):
        self.users[user_id]["preferred_category"] = category

    def upsert_seller(self, user_id, *business):
        self.sellers[user_id] = business
        self.set_role(user_id, "SELLER")

    def get_seller(self, user_id):
        return self.sellers.get(user_id)

    def create_product(self, user_id, name, category, description, price_kobo, stock):
        product_id = len(self.products) + 1
        self.products[product_id] = dict(
            product_id=product_id, seller_telegram_user_id=user_id, name=name,
            category=category, description=description, price_kobo=price_kobo,
            stock_quantity=stock, photo_object_key=None, business_name="Shop",
        )
        return product_id

    def get_product(self, product_id):
        return self.products.get(product_id)

    def set_product_photo(self, user_id, product_id, key):
        if self.fail_photo_update:
            raise RuntimeError("Database unavailable")
        self.products[product_id]["photo_object_key"] = key

    def set_stock(self, user_id, product_id, stock):
        product = self.products.get(product_id)
        if not product or product["seller_telegram_user_id"] != user_id:
            return False
        product["stock_quantity"] = stock
        return True

    def list_products(self, category=None, seller_id=None):
        return [p for p in self.products.values()
                if (category is None or p["category"] == category)
                and (seller_id is None or p["seller_telegram_user_id"] == seller_id)]


class FakeMedia:
    def __init__(self):
        self.objects = {}

    def put_product_photo(self, user_id, product_id, raw):
        key = f"products/{user_id}/{product_id}/photo.jpg"
        self.objects[key] = raw
        return key, "test-version"

    def get_photo(self, key):
        return self.objects[key]

    def delete_version(self, key, version_id):
        del self.objects[key]


class FakeMessage:
    def __init__(self, text=None, photo=None):
        self.text = text
        self.photo = photo or []
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(("text", text))

    async def reply_photo(self, photo, caption):
        self.replies.append(("photo", photo.read(), caption))


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self):
        self.answered = True


class FakePhoto:
    file_size = 4

    async def get_file(self):
        return self

    async def download_to_memory(self, out):
        out.write(b"\xff\xd8\xff\x00")


def update(message=None, user_id=42, query=None):
    return SimpleNamespace(
        message=message, callback_query=query, effective_message=message or query.message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type="private"),
    )


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()
        self.media = FakeMedia()
        self.bot = BotHandlers(self.store, self.media)
        self.context = SimpleNamespace(user_data={}, args=[])

    async def test_seller_product_photo_and_repeat_start(self):
        first = FakeMessage()
        self.assertEqual(await self.bot.start(update(first), self.context), CHOOSING)
        details = FakeMessage("Jane Doe, jane@example.com, +1234567890")
        self.assertEqual(await self.bot.choose(update(details), self.context), CLASS_STATE)
        choose = FakeQuery("role:seller", FakeMessage())
        self.assertEqual(await self.bot.classer(update(query=choose), self.context), SME_DETAILS)
        self.assertTrue(choose.answered)
        business = FakeMessage("Shop, shop@example.com, Main Street, +1234567890")
        self.assertEqual(await self.bot.seller_details(update(business), self.context),
                         ConversationHandler.END)
        self.assertEqual(self.store.users[42]["role"], "SELLER")

        second = FakeMessage()
        self.assertEqual(await self.bot.start(update(second), self.context),
                         ConversationHandler.END)
        self.assertEqual(len(self.store.users), 1)

        self.assertEqual(await self.bot.add_product(update(FakeMessage()), self.context),
                         PRODUCT_DETAILS)
        product = FakeMessage("Blue shirt | clothing | 19.99 | 5 | Cotton")
        self.assertEqual(await self.bot.product_details(update(product), self.context),
                         PRODUCT_PHOTO)
        photo = FakeMessage(photo=[FakePhoto()])
        self.assertEqual(await self.bot.photo_for_product(update(photo), self.context),
                         ConversationHandler.END)
        self.assertEqual(self.media.objects[self.store.products[1]["photo_object_key"]],
                         b"\xff\xd8\xff\x00")
        view = FakeMessage()
        self.context.args = ["1"]
        await self.bot.product(update(view), self.context)
        self.assertEqual(view.replies[0][0], "photo")

    async def test_invalid_details_retry_and_stock_ownership(self):
        self.assertEqual(await self.bot.choose(update(FakeMessage("bad")), self.context),
                         CHOOSING)
        self.assertEqual(self.store.users, {})
        self.store.upsert_user(42, "Jane", "jane@example.com", "+1234567890")
        self.store.upsert_seller(42, "Shop", "shop@example.com", "Street", "+1234567890")
        self.store.create_product(42, "Shirt", "Clothing/Fashion", None, 100, 2)
        self.context.args = ["1", "7"]
        other = FakeMessage()
        await self.bot.stock(update(other, user_id=99), self.context)
        self.assertEqual(self.store.products[1]["stock_quantity"], 2)
        self.assertIn("not yours", other.replies[0][1])

    async def test_failed_photo_db_update_removes_uploaded_version(self):
        self.store.upsert_user(42, "Jane", "jane@example.com", "+1234567890")
        self.store.upsert_seller(42, "Shop", "shop@example.com", "Street", "+1234567890")
        self.store.create_product(42, "Shirt", "Clothing/Fashion", None, 100, 2)
        self.store.fail_photo_update = True
        self.context.user_data["pending_product_id"] = 1
        message = FakeMessage(photo=[FakePhoto()])
        with self.assertLogs("handlers", level="ERROR"):
            state = await self.bot.photo_for_product(update(message), self.context)
        self.assertEqual(state, PRODUCT_PHOTO)
        self.assertEqual(self.media.objects, {})
        self.assertIsNone(self.store.products[1]["photo_object_key"])

    def test_product_validation(self):
        for text in ("x | clothing | -1 | 2 | d",
                     "x | clothing | 1.001 | 2 | d",
                     "x | unknown | 1 | 2 | d",
                     "x | clothing | 1 | -1 | d"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_product(text)
