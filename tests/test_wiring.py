"""End-to-end tests of the real handler wiring in main.build_application.

These feed synthetic Telegram updates through the real Application, SQLite store and
local media storage. Only the HTTP layer is replaced, so no token or network is needed.
Direct handler-method tests (test_bot.py) cannot see wiring problems such as a state
with no registered handler or two conversations interfering.
"""

import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from telegram import Update
from telegram.request import HTTPXRequest

import main
from handlers import BotHandlers
from local_backend import LocalMediaStorage, SQLiteStore


NAME_EMAIL = "Jane Doe, jane@example.com"
PHONE = "+1234567890"
BUSINESS = "Shop, shop@example.com, Main Street 1, Springfield, +1234567890"
PRODUCT = "Blue shirt | clothing | 19.99 | 5 | Cotton shirt"


class WiringTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []  # (api method, parameters) for every call to Telegram
        self.next_id = 0
        self.last_message_id = None

        async def fake_do_request(request, url, method, request_data=None, *args, **kwargs):
            api = url.rsplit("/", 1)[-1]
            params = dict(request_data.parameters) if request_data else {}
            self.sent.append((api, params))
            result = {
                "getMe": {"id": 1, "is_bot": True, "first_name": "B", "username": "test_bot"},
                "answerCallbackQuery": True,
            }.get(api) or {
                "message_id": len(self.sent), "date": 0,
                "chat": {"id": params.get("chat_id", 1), "type": "private"},
            }
            return 200, json.dumps({"ok": True, "result": result}).encode()

        patcher = patch.object(HTTPXRequest, "do_request", fake_do_request)
        patcher.start()
        self.addCleanup(patcher.stop)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SQLiteStore(Path(tmp.name) / "bot.sqlite3")
        self.media = LocalMediaStorage(Path(tmp.name) / "media")
        self.app = await self.make_app()

    async def make_app(self):
        settings = SimpleNamespace(backend="local", bot_token="123456:TEST-TOKEN")
        app = main.build_application(settings, self.store, self.media)
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        return app

    # -- helpers -----------------------------------------------------------------

    def _message(self, uid, chat_type="private", message_id=None, **fields):
        self.next_id += 1
        message_id = message_id or self.next_id
        self.last_message_id = message_id
        chat_id = uid if chat_type == "private" else -uid
        return {"message_id": message_id, "date": int(time.time()),
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": uid, "is_bot": False, "first_name": "T"}, **fields}

    def _update(self, **body):
        self.next_id += 1
        return Update.de_json({"update_id": self.next_id, **body}, self.app.bot)

    async def feed(self, update, app=None):
        """Process one update and return the texts the bot sent in response."""
        start = len(self.sent)
        await (app or self.app).process_update(update)
        return [params.get("text", "") for api, params in self.sent[start:]
                if api == "sendMessage"]

    async def say(self, uid, text, *, edited=False, message_id=None, chat_type="private",
                  app=None):
        """Send text. An edit reuses `message_id`, the ID of the message being edited."""
        entities = []
        if text.startswith("/"):
            entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        message = self._message(uid, chat_type, message_id, text=text, entities=entities)
        if edited:
            message["edit_date"] = int(time.time())
        key = "edited_message" if edited else "message"
        return await self.feed(self._update(**{key: message}), app)

    async def share_contact(self, uid, number, *, owner=None):
        contact = {"phone_number": number, "first_name": "T", "user_id": owner or uid}
        return await self.feed(self._update(message=self._message(uid, contact=contact)))

    async def tap(self, uid, data):
        """Tap an inline button; returns (texts sent, answerCallbackQuery params list)."""
        start = len(self.sent)
        update = self._update(callback_query={
            "id": str(self.next_id), "chat_instance": "x", "data": data,
            "from": {"id": uid, "is_bot": False, "first_name": "T"},
            "message": self._message(uid, text="menu")})
        await self.app.process_update(update)
        calls = self.sent[start:]
        texts = [p.get("text", "") for api, p in calls if api == "sendMessage"]
        answers = [p for api, p in calls if api == "answerCallbackQuery"]
        return texts, answers

    async def register(self, uid, role=None, category="art"):
        """Walk a new user through registration; optionally pick a role."""
        await self.say(uid, "/start")
        await self.say(uid, NAME_EMAIL)
        await self.say(uid, PHONE)
        if role == "seller":
            await self.tap(uid, "role:seller")
            await self.say(uid, BUSINESS)
        elif role == "customer":
            await self.tap(uid, "role:customer")
            await self.tap(uid, f"cat:{category}")

    def seed_seller(self, uid):
        self.store.upsert_user(uid, "Seller", "s@example.com", "+1234567890")
        self.store.upsert_seller(uid, "Shop", "shop@example.com", "Street 1", "+1234567890")

    def seed_photo(self, uid, product_id):
        key, version = self.media.put_product_photo(uid, product_id, b"\xff\xd8\xff.jpeg")
        self.store.set_product_photo(uid, product_id, key, version)
        return key


class JourneyTests(WiringTestCase):
    async def test_new_seller_registers_and_lists_a_product(self):
        welcome = await self.say(10, "/start")
        self.assertIn("Welcome to SMEbot", welcome[0])
        self.assertIn("full name and email", welcome[1])
        self.assertIn("phone number", (await self.say(10, NAME_EMAIL))[0])
        saved = await self.say(10, PHONE)
        self.assertIn("Phone number saved", saved[0])
        self.assertIn("seller or a customer", saved[1])
        texts, answers = await self.tap(10, "role:seller")
        self.assertIn("business", texts[0])
        self.assertEqual(len(answers), 1)
        self.assertIn("Seller profile saved", (await self.say(10, BUSINESS))[0])
        self.assertIn("name | category", (await self.say(10, "/addproduct"))[0])
        self.assertIn("Product #1 saved", (await self.say(10, PRODUCT))[0])
        self.assertIn("Done.", (await self.say(10, "/skip"))[0])
        self.assertEqual(self.store.get_user(10)["role"], "SELLER")
        self.assertEqual(self.store.get_seller(10)["address"], "Main Street 1, Springfield")
        self.assertEqual(len(self.store.list_products(seller_id=10)), 1)

    async def test_new_customer_chooses_a_category(self):
        await self.register(11)
        texts, _ = await self.tap(11, "role:customer")
        self.assertIn("Choose a category", texts[0])
        texts, _ = await self.tap(11, "cat:art")
        self.assertIn("Saved Art & Design", texts[0])
        self.assertEqual(self.store.get_user(11)["preferred_category"], "Art & Design")
        self.assertEqual(self.store.get_user(11)["role"], "CUSTOMER")

    async def test_repeat_start_does_not_duplicate_the_user(self):
        await self.register(12, "customer")
        self.assertIn("Welcome back", (await self.say(12, "/start"))[0])
        with self.store.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_commands_outside_a_conversation_still_work_mid_flow(self):
        self.seed_seller(13)
        await self.say(13, "/addproduct")
        self.assertIn("You have no products", (await self.say(13, "/myproducts"))[0])


class PhoneTests(WiringTestCase):
    """The phone number is either verified by Telegram or typed (suggestion 3.1)."""

    async def test_typed_number_is_stored_as_unverified(self):
        await self.register(50)
        user = self.store.get_user(50)
        self.assertEqual((user["phone"], user["phone_verified"]), (PHONE, False))

    async def test_number_shared_through_telegram_is_stored_as_verified(self):
        await self.say(51, "/start")
        await self.say(51, NAME_EMAIL)
        replies = await self.share_contact(51, "15550001234")
        self.assertIn("verified by Telegram", replies[0])
        user = self.store.get_user(51)
        self.assertEqual((user["phone"], user["phone_verified"]), ("+15550001234", True))
        self.assertIn("seller or a customer", replies[1])

    async def test_someone_elses_contact_is_rejected(self):
        await self.say(52, "/start")
        await self.say(52, NAME_EMAIL)
        replies = await self.share_contact(52, "15550001234", owner=999)
        self.assertIn("not your own", replies[0])
        self.assertIsNone(self.store.get_user(52))
        self.assertIn("seller or a customer", (await self.say(52, PHONE))[1])

    async def test_invalid_typed_number_asks_again(self):
        await self.say(53, "/start")
        await self.say(53, NAME_EMAIL)
        self.assertIn("valid phone", (await self.say(53, "call me maybe"))[0])
        self.assertIsNone(self.store.get_user(53))

    async def test_phone_prompt_offers_the_share_button(self):
        await self.say(54, "/start")
        await self.say(54, NAME_EMAIL)
        prompt = [p for api, p in self.sent if api == "sendMessage"][-1]
        self.assertIn("request_contact", json.dumps(prompt["reply_markup"]))

    async def test_profile_update_can_replace_a_verified_number_with_a_typed_one(self):
        await self.say(55, "/start")
        await self.say(55, NAME_EMAIL)
        await self.share_contact(55, "15550001234")
        await self.say(55, "/profile")
        await self.say(55, "Jane Doe, jane@example.com")
        await self.say(55, "+4400000000")
        user = self.store.get_user(55)
        self.assertEqual((user["phone"], user["phone_verified"]), ("+4400000000", False))


class PrivacyTests(WiringTestCase):
    """Users are told what is stored and can see and erase it (suggestions 3.1, 3.2)."""

    async def test_new_users_see_the_notice_before_giving_any_data(self):
        notice = (await self.say(60, "/start"))[0]
        for expected in ("What I store", "phone number", "/mydata", "/deleteme",
                         "until you erase it"):
            self.assertIn(expected, notice)
        self.assertEqual((await self.say(60, "/privacy"))[0], notice.split("\n\n", 1)[1])

    async def test_sellers_are_told_what_is_visible_to_others(self):
        await self.register(61)
        texts, _ = await self.tap(61, "role:seller")
        self.assertIn("visible to other users", texts[0])
        self.seed_seller(62)
        self.assertIn("visible to other users", (await self.say(62, "/sellerprofile"))[0])

    async def test_mydata_for_unregistered_user(self):
        self.assertIn("no data stored", (await self.say(63, "/mydata"))[0])

    async def test_mydata_shows_everything_stored_including_seller_details(self):
        await self.register(64, "seller")
        await self.say(64, "/addproduct")
        await self.say(64, PRODUCT)
        text = (await self.say(64, "/mydata"))[0]
        for expected in ("Jane Doe", "jane@example.com", PHONE, "typed by you", "seller",
                         "Shop", "shop@example.com", "Main Street 1, Springfield",
                         "Products listed: 1"):
            self.assertIn(expected, text)
        await self.say(64, "/profile")
        await self.say(64, NAME_EMAIL)
        await self.share_contact(64, "15550001234")
        self.assertIn("verified by Telegram", (await self.say(64, "/mydata"))[0])

    async def test_deleteme_asks_for_confirmation_and_no_keeps_everything(self):
        await self.register(65, "customer")
        prompt = (await self.say(65, "/deleteme"))[0]
        self.assertIn("permanently erases", prompt)
        self.assertEqual(self.store.get_user(65)["full_name"], "Jane Doe")
        texts, answers = await self.tap(65, "erase:no")
        self.assertEqual(texts, ["Nothing was erased."])
        self.assertEqual(len(answers), 1)
        self.assertEqual(self.store.get_user(65)["email"], "jane@example.com")

    async def test_deleteme_yes_erases_data_products_and_photos(self):
        await self.register(66, "seller")
        await self.say(66, "/addproduct")
        await self.say(66, PRODUCT)
        await self.say(66, "/skip")
        key = self.seed_photo(66, 1)
        await self.register(67, "customer")
        await self.say(66, "/deleteme")
        texts, _ = await self.tap(66, "erase:yes")
        self.assertIn("has been erased", texts[0])
        self.assertNotIn("could not be removed", texts[0])
        self.assertFalse((self.media.directory / key).exists())
        self.assertIsNone(self.store.get_user(66))
        self.assertIsNone(self.store.get_seller(66))
        self.assertEqual(self.store.list_products(), [])
        self.assertEqual(self.store.get_user(67)["full_name"], "Jane Doe")
        self.assertIn("no data stored", (await self.say(66, "/mydata"))[0])
        # An erased user is a stranger again and can start over.
        self.assertIn("Welcome to SMEbot", (await self.say(66, "/start"))[0])

    async def test_photo_deletion_failure_is_reported_and_logged(self):
        await self.register(68, "seller")
        await self.say(68, "/addproduct")
        await self.say(68, PRODUCT)
        key = self.seed_photo(68, 1)
        await self.say(68, "/deleteme")
        with patch.object(LocalMediaStorage, "delete_photo", side_effect=OSError("disk")), \
             self.assertLogs("handlers", level="ERROR") as logs:
            texts, _ = await self.tap(68, "erase:yes")
        self.assertIn("has been erased", texts[0])
        self.assertIn("1 product photo(s) could not be removed", texts[0])
        self.assertTrue(any(key in line for line in logs.output))
        self.assertIsNone(self.store.get_user(68))

    async def test_erase_failure_keeps_the_confirmation_open_for_a_retry(self):
        await self.register(69, "customer")
        await self.say(69, "/deleteme")
        with patch.object(SQLiteStore, "erase_user", side_effect=RuntimeError("db down")), \
             self.assertLogs("handlers", level="ERROR"):
            texts, _ = await self.tap(69, "erase:yes")
        self.assertIn("unavailable", texts[0])
        self.assertIsNotNone(self.store.get_user(69))
        texts, _ = await self.tap(69, "erase:yes")
        self.assertIn("has been erased", texts[0])

    async def test_deleteme_without_data_and_text_while_confirming(self):
        self.assertIn("no data stored", (await self.say(70, "/deleteme"))[0])
        await self.register(71, "customer")
        await self.say(71, "/deleteme")
        self.assertIn("tap a button", (await self.say(71, "yes please"))[0])
        self.assertIsNotNone(self.store.get_user(71))
        self.assertEqual(await self.say(71, "/cancel"), ["Cancelled."])


class EditTests(WiringTestCase):
    """An edit corrects the user's latest input and nothing else."""

    async def test_editing_a_rejected_message_moves_the_flow_on(self):
        await self.say(80, "/start")
        await self.say(80, "Jane Doe jane@example.com")  # missing comma: rejected
        original = self.last_message_id
        replies = await self.say(80, NAME_EMAIL, edited=True, message_id=original)
        self.assertIn("phone number", replies[0])
        self.assertIn("seller or a customer", (await self.say(80, PHONE))[1])

    async def test_editing_accepted_name_and_email_before_the_phone_step(self):
        await self.say(81, "/start")
        await self.say(81, "Jane Dow, jane@example.com")
        original = self.last_message_id
        self.assertIn("phone number", (await self.say(
            81, "Jane Doe, jane@example.com", edited=True, message_id=original))[0])
        await self.say(81, PHONE)
        self.assertEqual(self.store.get_user(81)["full_name"], "Jane Doe")

    async def test_editing_the_typed_phone_updates_it_while_the_role_is_pending(self):
        await self.say(82, "/start")
        await self.say(82, NAME_EMAIL)
        await self.say(82, "+1000000")
        original = self.last_message_id
        replies = await self.say(82, "+1999999999", edited=True, message_id=original)
        self.assertEqual(self.store.get_user(82)["phone"], "+1999999999")
        self.assertIn("seller or a customer", replies[-1])
        texts, _ = await self.tap(82, "role:customer")
        self.assertIn("Choose a category", texts[0])

    async def test_editing_business_details_after_they_were_saved(self):
        await self.register(83, "seller")
        original = self.last_message_id
        corrected = "Better Shop, shop@example.com, Main Street 1, Springfield, +1234567890"
        replies = await self.say(83, corrected, edited=True, message_id=original)
        self.assertEqual(replies, ["Seller profile updated."])
        self.assertEqual(self.store.get_seller(83)["business_name"], "Better Shop")

    async def test_editing_a_product_line_updates_the_product_instead_of_adding_one(self):
        self.seed_seller(84)
        await self.say(84, "/addproduct")
        await self.say(84, "Blue shirt | clothing | 19.99 | 5 | Cotton")
        original = self.last_message_id
        replies = await self.say(84, "Blue shirt | clothing | 24.50 | 9 | Cotton",
                                 edited=True, message_id=original)
        self.assertEqual(replies, ["Product #1 updated."])
        products = self.store.list_products(seller_id=84)
        self.assertEqual(len(products), 1)
        self.assertEqual((products[0]["price_kobo"], products[0]["stock_quantity"]), (2450, 9))
        # The product flow is still open, so the photo step keeps working.
        self.assertIn("Done.", (await self.say(84, "/skip"))[0])
        # A correction after the flow ended is still applied to the same product.
        await self.say(84, "Blue shirt | clothing | 30 | 9 | Cotton", edited=True,
                       message_id=original)
        self.assertEqual(self.store.get_product(1)["price_kobo"], 3000)

    async def test_an_invalid_product_correction_changes_nothing_and_keeps_the_photo_step(self):
        self.seed_seller(85)
        await self.say(85, "/addproduct")
        await self.say(85, "Blue shirt | clothing | 19.99 | 5 | Cotton")
        original = self.last_message_id
        replies = await self.say(85, "Blue shirt | clothing | cheap | 5 | Cotton",
                                 edited=True, message_id=original)
        self.assertIn("Try again", replies[0])
        self.assertEqual(self.store.get_product(1)["price_kobo"], 1999)
        self.assertIn("Done.", (await self.say(85, "/skip"))[0])

    async def test_editing_a_rejected_product_line_creates_it_once(self):
        self.seed_seller(86)
        await self.say(86, "/addproduct")
        await self.say(86, "Blue shirt | clothing | cheap | 5 | Cotton")
        original = self.last_message_id
        replies = await self.say(86, "Blue shirt | clothing | 19.99 | 5 | Cotton",
                                 edited=True, message_id=original)
        self.assertIn("Product #1 saved", replies[0])
        self.assertEqual(self.store.count_products(86), 1)

    async def test_only_the_latest_input_can_be_corrected(self):
        await self.say(87, "/start")
        await self.say(87, NAME_EMAIL)
        older = self.last_message_id
        await self.say(87, PHONE)
        self.assertEqual(await self.say(87, "Someone Else, else@example.com",
                                        edited=True, message_id=older), [])
        self.assertEqual(self.store.get_user(87)["full_name"], "Jane Doe")
        self.assertEqual(await self.say(87, "Totally unrelated message", edited=True), [])

    async def test_a_new_command_ends_the_chance_to_correct_the_old_input(self):
        self.seed_seller(88)
        await self.say(88, "/addproduct")
        await self.say(88, "Blue shirt | clothing | 19.99 | 5 | Cotton")
        original = self.last_message_id
        await self.say(88, "/addproduct")
        self.assertEqual(await self.say(88, "Blue shirt | clothing | 1 | 1 | x",
                                        edited=True, message_id=original), [])
        self.assertEqual(self.store.get_product(1)["price_kobo"], 1999)

    async def test_cancel_ends_the_chance_to_correct(self):
        await self.say(89, "/start")
        await self.say(89, NAME_EMAIL)
        original = self.last_message_id
        await self.say(89, "/cancel")
        self.assertEqual(await self.say(89, NAME_EMAIL, edited=True, message_id=original), [])
        self.assertIsNone(self.store.get_user(89))

    async def test_editing_after_choosing_a_role_is_ignored(self):
        await self.say(90, "/start")
        await self.say(90, NAME_EMAIL)
        await self.say(90, PHONE)
        phone_message = self.last_message_id
        await self.tap(90, "role:customer")
        self.assertEqual(await self.say(90, "+1888888888", edited=True,
                                        message_id=phone_message), [])
        self.assertEqual(self.store.get_user(90)["phone"], PHONE)


class ConversationIsolationTests(WiringTestCase):
    """A user is only ever in one conversation state (suggestion 1.2)."""

    async def test_profile_then_addproduct_accepts_the_product_line(self):
        self.seed_seller(20)
        await self.say(20, "/profile")
        await self.say(20, "/addproduct")
        reply = (await self.say(20, "Red hat | clothing | 9.50 | 3 | Wool"))[0]
        self.assertIn("Product #1 saved", reply)
        self.assertEqual(len(self.store.list_products(seller_id=20)), 1)

    async def test_one_cancel_clears_everything(self):
        self.seed_seller(21)
        await self.say(21, "/profile")
        await self.say(21, "/addproduct")
        self.assertEqual(await self.say(21, "/cancel"), ["Cancelled."])
        self.assertEqual(await self.say(21, PRODUCT), [])
        self.assertEqual(self.store.list_products(seller_id=21), [])

    async def test_abandoned_flow_does_not_capture_later_text(self):
        self.seed_seller(22)
        await self.say(22, "/addproduct")
        self.assertIn("Welcome back", (await self.say(22, "/start"))[0])
        self.assertEqual(await self.say(22, "hello"), [])
        self.assertEqual(self.store.list_products(seller_id=22), [])


class InputHandlingTests(WiringTestCase):
    """Unexpected input gets a reply or is ignored cleanly (suggestions 1.3, 1.4)."""

    async def test_invalid_details_ask_again_and_nothing_is_stored(self):
        await self.say(30, "/start")
        self.assertIn("Try again", (await self.say(30, "bad"))[0])
        self.assertIsNone(self.store.get_user(30))
        self.assertIn("phone number", (await self.say(30, NAME_EMAIL))[0])

    async def test_text_in_a_button_only_state_gets_a_hint(self):
        await self.say(31, "/start")
        await self.say(31, NAME_EMAIL)
        await self.say(31, PHONE)
        reply = (await self.say(31, "seller"))[0]
        self.assertIn("tap Seller or Customer", reply)
        self.assertIn("/cancel", reply)
        # The hint leaves the state alone, so the buttons still work afterwards.
        texts, _ = await self.tap(31, "role:customer")
        self.assertIn("Choose a category", texts[0])

    async def test_text_while_waiting_for_a_photo_gets_a_hint(self):
        self.seed_seller(32)
        await self.say(32, "/addproduct")
        await self.say(32, PRODUCT)
        reply = (await self.say(32, "here you go"))[0]
        self.assertIn("send a photo", reply)

    async def test_non_text_message_gets_a_hint(self):
        await self.say(33, "/start")
        update = self._update(message=self._message(
            33, location={"latitude": 1.0, "longitude": 2.0}))
        self.assertIn("full name and email", (await self.feed(update))[0])

    async def test_non_text_message_while_waiting_for_the_phone_gets_a_hint(self):
        await self.say(34, "/start")
        await self.say(34, NAME_EMAIL)
        update = self._update(message=self._message(
            34, location={"latitude": 1.0, "longitude": 2.0}))
        self.assertIn("share your Telegram number", (await self.feed(update))[0])

    async def test_stray_button_in_a_text_state_is_answered(self):
        await self.say(35, "/start")
        texts, answers = await self.tap(35, "cat:art")
        self.assertEqual(len(answers), 1)
        self.assertIn("full name and email", texts[0])

    async def test_edits_and_edited_commands_are_ignored_without_an_error(self):
        await self.say(36, "/start")
        with self.assertNoLogs(level=logging.ERROR):
            self.assertEqual(await self.say(36, NAME_EMAIL, edited=True), [])
            self.assertEqual(await self.say(36, "/start", edited=True), [])
        self.assertIsNone(self.store.get_user(36))

    async def test_stale_button_is_answered_with_an_expiry_notice(self):
        self.seed_seller(37)
        texts, answers = await self.tap(37, "cat:art")
        self.assertEqual(texts, [])
        self.assertEqual(len(answers), 1)
        self.assertIn("expired", answers[0]["text"])

    async def test_group_commands_get_a_private_chat_notice_and_start_nothing(self):
        reply = await self.say(38, "/start", chat_type="group")
        self.assertEqual(reply, ["Please message me in a private chat."])
        self.assertEqual(await self.say(38, NAME_EMAIL, chat_type="group"), [])
        self.assertIsNone(self.store.get_user(38))

    async def test_group_ignores_commands_for_other_bots(self):
        self.assertEqual(await self.say(39, "/start@other_bot", chat_type="group"), [])


class FailureHandlingTests(WiringTestCase):
    async def test_storage_failure_keeps_the_state_so_the_user_can_retry(self):
        await self.say(40, "/start")
        await self.say(40, NAME_EMAIL)
        with patch.object(SQLiteStore, "upsert_user", side_effect=RuntimeError("db down")), \
             self.assertLogs("handlers", level="ERROR"):
            reply = (await self.say(40, PHONE))[0]
        self.assertIn("unavailable", reply)
        self.assertIsNone(self.store.get_user(40))
        self.assertIn("seller or a customer", (await self.say(40, PHONE))[1])

    async def test_unexpected_exception_is_logged_and_the_user_is_told(self):
        async def broken(self, update, context):
            raise RuntimeError("boom")

        with patch.object(BotHandlers, "preferences", broken):
            app = await self.make_app()
        with self.assertLogs("handlers", level="ERROR") as logs:
            replies = await self.say(41, "/preferences", app=app)
        self.assertEqual(replies, ["Something went wrong. Please try again."])
        self.assertIn("Unhandled error", logs.output[0])
        # The failure must not leave the user stuck in a conversation state.
        self.assertIn("Welcome to SMEbot", (await self.say(41, "/start", app=app))[0])


class StartupTests(unittest.TestCase):
    def test_httpx_logger_cannot_leak_the_bot_token(self):
        logger = logging.getLogger("httpx")
        previous = logger.level
        self.addCleanup(logger.setLevel, previous)
        logger.setLevel(logging.NOTSET)
        with patch("logging.basicConfig"):
            main.configure_logging()
        self.assertFalse(logger.isEnabledFor(logging.INFO))

    def test_polling_only_requests_the_update_types_the_bot_handles(self):
        application = MagicMock()
        with patch.object(main, "configure_logging"), \
             patch.object(main, "build_application", return_value=application):
            main.main()
        application.run_polling.assert_called_once_with(
            allowed_updates=["message", "edited_message", "callback_query"])


if __name__ == "__main__":
    unittest.main()
