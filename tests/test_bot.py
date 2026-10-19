import unittest
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from fakes import FakeMedia, FakeStore
from handlers import (
    BotHandlers, CHOOSING, CLASS_STATE, PHONE, SME_DETAILS, PRODUCT_DETAILS,
    CONFIRM_ERASE,
    PRODUCT_PHOTO, parse_business, parse_name_email, parse_product,
    parse_telegram_phone, clean_phone,
)


class FakeMessage:
    def __init__(self, text=None, photo=None, message_id=1):
        self.text = text
        self.photo = photo or []
        self.message_id = message_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(("text", text))

    async def reply_photo(self, photo, caption, **kwargs):
        self.replies.append(("photo", photo if isinstance(photo, str) else photo.read(), caption))
        return SimpleNamespace(photo=[FakePhoto()])


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class FakePhoto:
    file_size = 4
    file_id = "telegram-photo"
    width = height = 100

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
        self.context = SimpleNamespace(user_data={}, args=[], bot=SimpleNamespace(id=1))

    async def register(self, user_id=42, phone="+1234567890"):
        await self.bot.choose(update(FakeMessage("Jane Doe, jane@example.com"), user_id), self.context)
        await self.bot.phone_text(update(FakeMessage(phone), user_id), self.context)

    async def test_erasure_marker_failure_keeps_live_data(self):
        await self.register()

        class FailedLedger:
            def record_erasure(self, user_id):
                raise RuntimeError("recovery storage unavailable")

        self.bot.erasures = FailedLedger()
        message = FakeMessage()
        result = await self.bot.confirm_erase(
            update(query=FakeQuery("erase:yes", message)), self.context)
        self.assertEqual(result, CONFIRM_ERASE)
        self.assertIsNotNone(self.store.get_user(42))
        self.assertIn("could not start", message.replies[0][1])

    async def test_limited_compact_create_and_photo_preserve_flow(self):
        from abuse_limits import AbuseLimits
        from unittest.mock import AsyncMock
        await self.register()
        self.store.upsert_seller(42, 'Shop', 's@example.com', 'Online', '+1234567890')
        self.store.create_product(42, 'First', 'art', None, 100, 2)
        self.store.abuse_limits = AbuseLimits(enabled=True, listings_daily=1, photos_daily=1)
        message = FakeMessage('Second | art | 1 | 2 | desc')
        self.assertEqual(await self.bot.product_details(update(message), self.context), PRODUCT_DETAILS)
        self.assertIn('Daily listing limit', message.replies[0][1])
        self.context.user_data['pending_product_id'] = 1
        self.store.admit_photo_upload(42, 1)
        photo = FakePhoto()
        photo.get_file = AsyncMock()
        message = FakeMessage(photo=[photo])
        self.assertEqual(await self.bot.photo_for_product(update(message), self.context), PRODUCT_PHOTO)
        photo.get_file.assert_not_called()
        self.assertEqual(self.context.user_data['pending_product_id'], 1)
        self.assertEqual(self.store.abuse_summary(42)['photo_attempt_count'], 1)

    async def test_seller_product_photo_and_repeat_start(self):
        first = FakeMessage()
        self.assertEqual(await self.bot.start(update(first), self.context), CLASS_STATE)
        self.assertIn("seller or a buyer", first.replies[0][1])
        choose = FakeQuery("role:seller", FakeMessage())
        self.assertEqual(await self.bot.classer(update(query=choose), self.context), CHOOSING)
        self.assertIn("What I store", choose.message.replies[0][1])
        self.assertTrue(choose.answered)
        details = FakeMessage("Jane Doe, jane@example.com")
        self.assertEqual(await self.bot.choose(update(details), self.context), PHONE)
        phone = FakeMessage("+1234567890")
        self.assertEqual(await self.bot.phone_text(update(phone), self.context), SME_DETAILS)
        self.assertIs(self.store.users[42]["phone_verified"], False)
        self.assertEqual(self.store.users[42]["role"], "SELLER")
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
        self.assertEqual(self.store.products[1]["photo_version_id"], "test-version")
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
        self.store.create_product(42, "Shirt", "clothing", None, 100, 2)
        self.context.args = ["1", "7"]
        other = FakeMessage()
        await self.bot.stock(update(other, user_id=99), self.context)
        self.assertEqual(self.store.products[1]["stock_quantity"], 2)
        self.assertIn("not yours", other.replies[0][1])

    async def test_invalid_phone_asks_again_and_stores_nothing(self):
        await self.bot.choose(update(FakeMessage("Jane Doe, jane@example.com")), self.context)
        self.assertEqual(
            await self.bot.phone_text(update(FakeMessage("call me")), self.context), PHONE)
        self.assertEqual(self.store.users, {})

    async def test_phone_step_without_a_draft_restarts_politely(self):
        message = FakeMessage("+1234567890")
        self.assertEqual(await self.bot.phone_text(update(message), self.context),
                         ConversationHandler.END)
        self.assertIn("/start", message.replies[0][1])

    async def test_telegram_contact_is_verified_only_when_it_is_the_users_own(self):
        self.context.user_data["signup_role"] = "CUSTOMER"
        await self.bot.choose(update(FakeMessage("Jane Doe, jane@example.com")), self.context)
        foreign = FakeMessage()
        foreign.contact = SimpleNamespace(user_id=7, phone_number="15550001")
        self.assertEqual(await self.bot.phone_contact(update(foreign), self.context), PHONE)
        self.assertEqual(self.store.users, {})
        own = FakeMessage()
        own.contact = SimpleNamespace(user_id=42, phone_number="15550001")
        self.assertEqual(await self.bot.phone_contact(update(own), self.context),
                         ConversationHandler.END)
        self.assertEqual(self.store.users[42]["phone"], "+15550001")
        self.assertIs(self.store.users[42]["phone_verified"], True)

    async def test_failed_photo_db_update_removes_uploaded_version(self):
        self.store.upsert_user(42, "Jane", "jane@example.com", "+1234567890")
        self.store.upsert_seller(42, "Shop", "shop@example.com", "Street", "+1234567890")
        self.store.create_product(42, "Shirt", "clothing", None, 100, 2)
        self.store.fail_photo_update = True
        self.context.user_data["pending_product_id"] = 1
        message = FakeMessage(photo=[FakePhoto()])
        with self.assertLogs("handlers", level="ERROR"):
            state = await self.bot.photo_for_product(update(message), self.context)
        self.assertEqual(state, PRODUCT_PHOTO)
        self.assertEqual(self.media.objects, {})
        self.assertIsNone(self.store.products[1]["photo_object_key"])

    async def test_product_price_is_usd(self):
        self.store.upsert_user(42, "Jane", "jane@example.com", "+1234567890")
        self.store.upsert_seller(42, "Shop", "shop@example.com", "Street", "+1234567890")
        prompt = FakeMessage()
        self.assertEqual(await self.bot.add_product(update(prompt), self.context), PRODUCT_DETAILS)
        self.assertIn("price in USD", prompt.replies[0][1])
        self.store.create_product(42, "Shirt", "clothing", None, 1999, 2)
        self.context.args = ["1"]
        shown = FakeMessage()
        await self.bot.product(update(shown), self.context)
        self.assertIn("Price: USD 19.99", shown.replies[0][1])

    def test_product_validation(self):
        for text in ("x | clothing | -1 | 2 | d",
                     "x | clothing | 1.001 | 2 | d",
                     "x | unknown | 1 | 2 | d",
                     "x | clothing | 1 | -1 | d"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_product(text)


class ParserTests(unittest.TestCase):
    def test_name_may_contain_commas(self):
        self.assertEqual(parse_name_email("Doe, Jane,  jane@example.com "),
                         ("Doe, Jane", "jane@example.com"))

    def test_name_and_email_errors(self):
        for text in ("Jane Doe", "Jane Doe, not-an-email", ", jane@example.com", "x" * 161 + ",a@b.co"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_name_email(text)

    def test_business_address_may_contain_commas(self):
        self.assertEqual(
            parse_business("Shop, shop@example.com, 1 Main St, Springfield, +1234567890"),
            ("Shop", "shop@example.com", "1 Main St, Springfield", "+1234567890"))

    def test_private_seller_reuses_account_contact(self):
        self.assertEqual(
            parse_business("Jane's finds | Online", "jane@example.com", "+1234567890"),
            ("Jane's finds", "jane@example.com", "Online", "+1234567890"))

    def test_seller_can_use_separate_contact(self):
        self.assertEqual(
            parse_business("Market stall | Belgrade | shop@example.com | +19876543210",
                           "jane@example.com", "+1234567890"),
            ("Market stall", "shop@example.com", "Belgrade", "+19876543210"))

    def test_business_errors(self):
        for text in ("Shop, shop@example.com, +1234567890",
                     "Shop, bad, Street, +1234567890",
                     "Shop, shop@example.com, Street, abc",
                     "Shop, shop@example.com, " + "x" * 401 + ", +1234567890",
                     "Name | ", "Name | Belgrade | bad | +1234567890",
                     "Name | Belgrade | shop@example.com | bad"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_business(text, "jane@example.com", "+1234567890")

    def test_telegram_numbers_get_a_plus_prefix(self):
        self.assertEqual(parse_telegram_phone("15550001234"), "+15550001234")
        self.assertEqual(parse_telegram_phone("+15550001234"), "+15550001234")
        with self.assertRaises(ValueError):
            parse_telegram_phone("12")

    def test_typed_phone_validation(self):
        self.assertEqual(clean_phone(" +1 (555) 000-1234 "), "+1 (555) 000-1234")
        with self.assertRaises(ValueError):
            clean_phone("555-CALL-NOW")
