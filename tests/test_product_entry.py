"""Guided listing validation and journeys through the real Telegram application."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import PhotoSize
from telegram.error import NetworkError
from telegram.request import HTTPXRequest

from catalog import CATEGORIES
from handlers import (clean_name, clean_product_category, clean_product_price,
                      clean_product_stock, clean_product_description, parse_product)
from test_wiring import WiringTestCase, PRODUCT, NAME_EMAIL, PHONE


class ProductFieldTests(unittest.TestCase):
    def test_price_boundaries_and_exact_cents(self):
        for text, expected in (("0", 0), ("19.99", 1999), ("9999999999.99", 999999999999),
                               (" 1.20 ", 120), ("1.0000", 100), ("1e2", 10000)):
            with self.subTest(text=text):
                self.assertEqual(clean_product_price(text), expected)
                self.assertEqual(parse_product(f"Hat | art | {text} | 0 | ")[3], expected)
        for text in ("", "cheap", "-0.01", "10000000000", "1.001", "NaN", "sNaN",
                     "Infinity", "-Infinity", "1e100000", "1e-100000",
                     "0.01000000000000000000000000000000001"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                clean_product_price(text)

    def test_stock_boundaries(self):
        self.assertEqual(clean_product_stock("0"), 0)
        self.assertEqual(clean_product_stock("9999999999"), 9999999999)
        for text in ("", "-1", "10000000000", "1.5", "1.0", "NaN", "Infinity", "1e2"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                clean_product_stock(text)

    def test_text_and_category_boundaries(self):
        self.assertEqual(clean_name(" " + "n" * 160 + " "), "n" * 160)
        for text in ("", "  ", "n" * 161):
            with self.subTest(text=text), self.assertRaises(ValueError):
                clean_name(text)
        self.assertIsNone(clean_product_description("  "))
        self.assertEqual(clean_product_description("x" * 1000), "x" * 1000)
        with self.assertRaises(ValueError):
            clean_product_description("x" * 1001)
        for key in CATEGORIES:
            self.assertEqual(clean_product_category(key.upper()), key)
        for key in ("unknown", "Art & Design", ""):
            with self.subTest(key=key), self.assertRaises(ValueError):
                clean_product_category(key)


class ProductEntryTests(WiringTestCase):
    def button(self, label):
        # Match the simulated Telegram response message ID, not an incoming update ID.
        for message_id, (api, params) in reversed(list(enumerate(self.sent, 1))):
            if api != "sendMessage":
                continue
            for row in params.get("reply_markup", {}).get("inline_keyboard", []):
                for button in row:
                    if button["text"] == label:
                        data = button["callback_data"]
                        self.assertLessEqual(len(data.encode()), 64)
                        return data, message_id
        self.fail(f"No button: {label}")

    async def press(self, button, uid=100, *, chat_type="private"):
        data, message_id = button
        start = len(self.sent)
        texts = await self.feed(self._update(callback_query={
            "id": str(self.next_id), "chat_instance": "x", "data": data,
            "from": {"id": uid, "is_bot": False, "first_name": "T"},
            "message": self._message(uid, chat_type=chat_type, message_id=message_id, text="prompt")}))
        answers = [params for api, params in self.sent[start:] if api == "answerCallbackQuery"]
        self.assertEqual(len(answers), 1)
        return texts

    async def start_entry(self, uid=100, menu=False):
        self.seed_seller(uid)
        if menu:
            return (await self.tap(uid, "menu:addproduct"))[0]
        return await self.say(uid, "/addproduct")

    async def through(self, stage="review", uid=100, *, skip=False):
        await self.start_entry(uid)
        if stage == "name":
            return
        await self.say(uid, "Blue shirt")
        if stage == "category":
            return
        await self.press(self.button(CATEGORIES["clothing"]), uid)
        if stage == "price":
            return
        await self.say(uid, "19.99")
        if stage == "stock":
            return
        await self.say(uid, "5")
        if stage == "description":
            return
        return await self.say(uid, "/skip" if skip else "Cotton shirt")

    def count(self, uid=100):
        return self.store.count_products(uid)

    async def test_complete_guided_entry_saves_only_after_review(self):
        review = await self.through()
        self.assertEqual(self.count(), 0)
        for value in ("not saved yet", "Blue shirt", CATEGORIES["clothing"], "USD 19.99",
                      "Stock: 5", "Cotton shirt"):
            self.assertIn(value, review[0])
        saved = self.button("Save product")
        texts = await self.press(saved)
        self.assertIn("Your product #1 saved (catalog ID #1)", texts[0])
        self.assertIn("Cancelling now leaves", texts[0])
        product = self.store.get_product(1)
        self.assertEqual(tuple(product[k] for k in
                               ("name", "category", "price_minor", "stock_quantity", "description")),
                         ("Blue shirt", "clothing", 1999, 5, "Cotton shirt"))
        self.assertNotIn("product_draft", self.app.user_data[100])
        self.assertNotIn("last_input", self.app.user_data[100])
        self.assertIn("expired", (await self.press(saved))[0])
        self.assertEqual(self.count(), 1)
        self.assertIn("Done", (await self.say(100, "/skip"))[0])

    async def test_menu_entry_empty_description_and_zero_price_and_stock(self):
        await self.start_entry(menu=True)
        await self.say(100, "Free pattern")
        await self.press(self.button(CATEGORIES["art"]))
        await self.say(100, "0")
        await self.say(100, "0")
        texts = await self.say(100, "/skip")
        self.assertIn("Description: (none)", texts[0])
        self.assertEqual(self.count(), 0)
        await self.press(self.button("Save product"))
        self.assertEqual(self.store.get_product(1)["price_minor"], 0)
        self.assertEqual(self.store.get_product(1)["stock_quantity"], 0)
        self.assertIsNone(self.store.get_product(1)["description"])
        await self.say(100, "/cancel")
        self.assertEqual(self.count(), 1)

    async def test_field_errors_retry_without_partial_listings(self):
        await self.start_entry()
        for name in ("  ", "x" * 161):
            self.assertIn("name", (await self.say(100, name))[0])
        await self.say(100, "Hat")
        self.assertIn("choose a product category", (await self.say(100, "unknown"))[0])
        await self.press(self.button(CATEGORIES["art"]))
        for price in ("-1", "1.001", "NaN", "Infinity", "oops"):
            self.assertIn("USD price", (await self.say(100, price))[0])
            self.assertEqual(self.count(), 0)
        await self.say(100, "1.20")
        for stock in ("-1", "1.1", "oops"):
            self.assertIn("whole-number stock", (await self.say(100, stock))[0])
        await self.say(100, "1")
        self.assertIn("1000", (await self.say(100, "x" * 1001))[0])
        await self.say(100, "Fine")
        await self.press(self.button("Save product"))
        self.assertEqual(self.count(), 1)

    async def test_latest_name_edit_invalidates_category_button(self):
        await self.start_entry()
        await self.say(100, "Hat")
        original = self.last_message_id
        old_category = self.button(CATEGORIES["art"])
        await self.say(100, "Cap", edited=True, message_id=original)
        current = self.button(CATEGORIES["art"])
        self.assertNotEqual(old_category, current)
        self.assertIn("expired", (await self.press(old_category))[0])
        await self.press(current)
        self.assertEqual(await self.say(100, "Not applied", edited=True, message_id=original), [])
        self.assertEqual(self.app.user_data[100]["product_draft"]["fields"]["name"], "Cap")

    async def test_price_stock_and_review_edits_revalidate_and_invalidate_summary(self):
        await self.through("stock")
        price_message = self.last_message_id
        self.assertIn("whole-number stock", (await self.say(
            100, "24.50", edited=True, message_id=price_message))[0])
        await self.say(100, "7")
        stock_message = self.last_message_id
        self.assertEqual(await self.say(100, "1", edited=True, message_id=price_message), [])
        self.assertIn("whole-number stock", (await self.say(
            100, "-1", edited=True, message_id=stock_message))[0])
        await self.say(100, "9", edited=True, message_id=stock_message)
        await self.say(100, "Old description")
        description_message = self.last_message_id
        old_save = self.button("Save product")
        await self.say(100, "New description", edited=True, message_id=description_message)
        new_save = self.button("Save product")
        self.assertIn("expired", (await self.press(old_save))[0])
        self.assertEqual(self.count(), 0)
        await self.press(new_save)
        product = self.store.get_product(1)
        self.assertEqual((product["price_minor"], product["stock_quantity"], product["description"]),
                         (2450, 9, "New description"))
        self.assertEqual(await self.say(100, "Ignored", edited=True, message_id=description_message), [])
        self.assertEqual(self.store.get_product(1)["description"], "New description")

    async def test_invalid_description_edit_revokes_save_and_can_be_corrected(self):
        await self.through()
        original = self.last_message_id
        save = self.button("Save product")
        self.assertIn("1000", (await self.say(100, "x" * 1001, edited=True, message_id=original))[0])
        self.assertIn("expired", (await self.press(save))[0])
        await self.say(100, "Corrected", edited=True, message_id=original)
        await self.press(self.button("Save product"))
        self.assertEqual(self.store.get_product(1)["description"], "Corrected")

    async def test_cancel_at_every_step_discards_draft_and_buttons(self):
        for stage in ("name", "category", "price", "stock", "description", "review"):
            with self.subTest(stage=stage):
                await self.through(stage)
                buttons = [self.button("Save product")] if stage == "review" else []
                self.assertIn("Cancelled", (await self.say(100, "/cancel"))[0])
                self.assertNotIn("product_draft", self.app.user_data[100])
                for button in buttons:
                    self.assertIn("expired", (await self.press(button))[0])
                self.assertEqual(self.count(), 0)

    async def test_restart_and_cancel_review_buttons(self):
        await self.through()
        old_save = self.button("Save product")
        restart = self.button("Restart entry")
        self.assertIn("product name", (await self.press(restart))[0])
        self.assertEqual(self.app.user_data[100]["product_draft"]["fields"], {})
        self.assertIn("expired", (await self.press(old_save))[0])
        await self.through()
        save = self.button("Save product")
        self.assertIn("Cancelled", (await self.press(self.button("Cancel")))[0])
        self.assertIn("expired", (await self.press(save))[0])
        self.assertEqual(self.count(), 0)

    async def test_new_entry_command_at_every_step_discards_draft(self):
        for stage in ("name", "category", "price", "stock", "description", "review"):
            with self.subTest(stage=stage):
                await self.through(stage)
                await self.say(100, "/addproduct")
                self.assertEqual(self.app.user_data[100]["product_draft"]["fields"], {})
                self.assertNotIn("last_input", self.app.user_data[100])
                self.assertEqual(self.count(), 0)

    async def test_old_button_does_not_interrupt_another_conversation(self):
        await self.through()
        old_save = self.button("Save product")
        await self.say(100, "/profile")
        self.assertIn("expired", (await self.press(old_save))[0])
        await self.say(100, NAME_EMAIL)
        await self.say(100, PHONE)
        self.assertEqual(self.store.get_user(100)["full_name"], "Jane Doe")
        self.assertEqual(self.count(), 0)

    async def test_callbacks_validate_user_message_action_and_size(self):
        await self.through()
        save = self.button("Save product")
        draft = copy.deepcopy(self.app.user_data[100]["product_draft"])
        self.assertIn("expired", (await self.press(save, uid=200))[0])
        self.assertEqual(await self.press(save, chat_type="group"), [])
        for data, mid in ((save[0], save[1] + 10000), ("entry:broken", save[1]),
                          (save[0] + "x" * 65, save[1]),
                          (save[0].replace(":save", ":unknown"), save[1]),
                          (save[0].replace(":save", ":art"), save[1])):
            with self.subTest(data=data):
                self.assertEqual(len(await self.press((data, mid))), 1)
                self.assertEqual(self.app.user_data[100]["product_draft"], draft)
                self.assertEqual(self.count(), 0)
        await self.press(save)
        self.assertEqual(self.count(), 1)

    async def test_category_rejects_unknown_and_repeated_actions(self):
        await self.through("category")
        category = self.button(CATEGORIES["art"])
        bad = (category[0].rsplit(":", 1)[0] + ":unknown", category[1])
        await self.press(bad)
        self.assertNotIn("category", self.app.user_data[100]["product_draft"]["fields"])
        self.assertIn("price in USD", (await self.press(category))[0])
        self.assertIn("expired", (await self.press(category))[0])

    async def test_application_restart_expires_unsaved_drafts(self):
        for stage, label in (("category", CATEGORIES["art"]), ("review", "Save product")):
            with self.subTest(stage=stage):
                await self.through(stage)
                button = self.button(label)
                self.app = await self.make_app()
                self.assertIn("expired", (await self.press(button))[0])
                self.assertEqual(self.count(), 0)
                self.assertIn("product name", (await self.say(100, "/addproduct"))[0])

    async def test_seller_required_at_start_and_rechecked_before_save(self):
        self.assertIn("/sellerprofile", (await self.say(100, "/addproduct"))[0])
        await self.through()
        save = self.button("Save product")
        self.store.erase_user(100)
        self.assertIn("/sellerprofile", (await self.press(save))[0])
        self.assertEqual(self.count(), 0)
        self.assertNotIn("product_draft", self.app.user_data[100])

    async def test_save_failure_keeps_review_and_does_not_automatically_retry(self):
        await self.through()
        old_save = self.button("Save product")
        with patch.object(self.store, "create_product", side_effect=RuntimeError("offline")) as create:
            with self.assertLogs("handlers", level="ERROR"):
                texts = await self.press(old_save)
            self.assertIn("Check /myproducts before retrying", texts[0])
            self.assertEqual(create.call_count, 1)
            self.assertEqual(self.count(), 0)
        new_save = self.button("Save product")
        self.assertNotEqual(old_save, new_save)
        self.assertIn("expired", (await self.press(old_save))[0])
        await self.press(new_save)
        self.assertEqual(self.count(), 1)

    async def test_failed_success_reply_cannot_repeat_a_known_successful_insert(self):
        await self.through()
        save = self.button("Save product")
        original = HTTPXRequest.do_request
        failed = False

        async def fail_once(request, url, method, request_data=None, **kwargs):
            nonlocal failed
            if url.endswith("/sendMessage") and not failed:
                failed = True
                raise NetworkError("simulated send failure")
            return await original(request, url, method, request_data, **kwargs)

        with patch.object(HTTPXRequest, "do_request", fail_once):
            with self.assertLogs("handlers", level="ERROR"):
                texts = await self.press(save)
        self.assertIn("unavailable", texts[0])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.app.user_data[100]["pending_product_id"], 1)
        self.assertIn("expired", (await self.press(save))[0])
        self.assertEqual(self.count(), 1)
        self.assertIn("Done", (await self.say(100, "/skip"))[0])

    async def test_catalog_navigation_preserves_the_entire_draft(self):
        await self.through()
        save = self.button("Save product")
        draft = copy.deepcopy(self.app.user_data[100]["product_draft"])
        await self.say(100, "/products all")
        await self.tap(100, "prodpage:mine:all:new")
        self.assertEqual(self.app.user_data[100]["product_draft"], draft)
        await self.press(save)
        self.assertEqual(self.count(), 1)

    async def test_optional_photo_retry_uses_the_already_saved_product(self):
        await self.through(skip=True)
        await self.press(self.button("Save product"))

        def photo_update():
            return self._update(message=self._message(100, photo=[dict(
                file_id="incoming", file_unique_id="unique", width=100, height=100, file_size=4)]))

        async def download(out):
            out.write(b"\xff\xd8\xff\x00")

        incoming_file = SimpleNamespace(download_to_memory=download)
        with patch.object(PhotoSize, "get_file", AsyncMock(return_value=incoming_file)):
            with patch.object(self.media, "put_product_photo", side_effect=RuntimeError("offline")):
                with self.assertLogs("handlers", level="ERROR"):
                    self.assertIn("unavailable", (await self.feed(photo_update()))[0])
            self.assertEqual(self.app.user_data[100]["pending_product_id"], 1)
            self.assertIn("Photo saved", (await self.feed(photo_update()))[0])
        self.assertEqual(self.count(), 1)
        self.assertIsNotNone(self.store.get_product(1)["photo_object_key"])
        self.assertNotIn("pending_product_id", self.app.user_data[100])

    async def test_compact_shortcut_remains_immediate_and_correctable(self):
        await self.start_entry()
        self.assertIn("saved", (await self.say(100, PRODUCT))[0])
        message_id = self.last_message_id
        self.assertEqual(self.count(), 1)
        self.assertNotIn("product_draft", self.app.user_data[100])
        await self.say(100, "Hat | art | 1 | 2 | New", edited=True, message_id=message_id)
        self.assertEqual(self.store.get_product(1)["name"], "Hat")
        self.assertEqual(self.count(), 1)
