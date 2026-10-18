"""Catalog paging through real PTB wiring, SQLite and simulated Telegram HTTP."""

import re
from unittest.mock import patch

from telegram.error import NetworkError
from telegram.request import HTTPXRequest

from test_wiring import WiringTestCase, NAME_EMAIL, PHONE, PRODUCT


class CatalogPaginationTests(WiringTestCase):
    def seed_products(self, seller=100, count=45, category="art"):
        self.seed_seller(seller)
        return [self.store.create_product(seller, f"Item {seller}-{n}", category, None, 100, 5)
                for n in range(count)]

    @staticmethod
    def catalog_ids(texts):
        return [int(raw) for raw in re.findall(r"^#([0-9]+) ", "\n".join(texts), re.M)]

    def buttons(self):
        messages = [p for api, p in self.sent if api == "sendMessage"]
        markup = messages[-1].get("reply_markup", {})
        buttons = [b for row in markup.get("inline_keyboard", []) for b in row]
        for button in buttons:
            self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)
        return {b["text"]: b["callback_data"] for b in buttons}

    async def test_all_45_products_are_reachable_and_newest_returns_to_start(self):
        ids = self.seed_products()
        texts = await self.say(101, "/products all")
        seen = self.catalog_ids(texts)
        self.assertEqual(seen, list(reversed(ids[25:])))
        self.assertEqual(len(texts), 5)  # four groups of five plus navigation/guidance
        self.assertEqual(set(self.buttons()), {"Older products"})
        for expected in (list(reversed(ids[5:25])), list(reversed(ids[:5]))):
            texts, answers = await self.tap(101, self.buttons()["Older products"])
            self.assertEqual(len(answers), 1)
            self.assertEqual(self.catalog_ids(texts), expected)
            self.assertEqual(len(texts), (len(expected) + 4) // 5 + 1)
            seen.extend(self.catalog_ids(texts))
        self.assertEqual(seen, list(reversed(ids)))
        self.assertEqual(set(self.buttons()), {"Newest products"})
        texts, answers = await self.tap(101, self.buttons()["Newest products"])
        self.assertEqual(self.catalog_ids(texts), list(reversed(ids[25:])))
        self.assertEqual(len(answers), 1)
        self.assertNotIn("Newest products", self.buttons())

    async def test_exactly_twenty_has_no_older_button(self):
        ids = self.seed_products(count=20)
        texts = await self.say(101, "/products all")
        self.assertEqual(self.catalog_ids(texts), list(reversed(ids)))
        self.assertEqual(self.buttons(), {})
        self.store.create_product(100, "One more", "art", None, 100, 1)
        await self.say(101, "/products all")
        texts, _ = await self.tap(101, self.buttons()["Older products"])
        self.assertEqual(self.catalog_ids(texts), ids[:1])
        self.assertEqual(set(self.buttons()), {"Newest products"})

    async def test_category_stays_selected_after_preference_changes(self):
        art = self.seed_products(count=23)
        clothing = self.seed_products(seller=200, count=23, category="clothing")
        self.store.upsert_user(101, "Buyer", "b@example.com", PHONE)
        self.store.set_preference(101, "art")
        texts = await self.say(101, "/products")
        self.assertEqual(self.catalog_ids(texts), list(reversed(art[3:])))
        older = self.buttons()["Older products"]
        self.store.set_preference(101, "clothing")
        texts, _ = await self.tap(101, older)
        self.assertEqual(self.catalog_ids(texts), list(reversed(art[:3])))
        texts, _ = await self.tap(101, self.buttons()["Newest products"])
        self.assertEqual(self.catalog_ids(texts), list(reversed(art[3:])))
        # A fresh browse resolves the current preference; explicit all overrides it.
        texts = await self.say(101, "/products")
        self.assertEqual(self.catalog_ids(texts), list(reversed(clothing[3:])))
        await self.say(101, "/products all")
        self.assertIn(":all:all:", self.buttons()["Older products"])
        await self.say(101, "/products art")
        self.assertIn(":all:art:", self.buttons()["Older products"])

    async def test_category_menu_and_seller_menu_offer_pagination(self):
        ids = self.seed_products(count=21)
        await self.tap(101, "menu:products")
        texts, answers = await self.tap(101, "browse:art")
        self.assertEqual(self.catalog_ids(texts), list(reversed(ids[1:])))
        self.assertEqual(len(answers), 1)
        texts, _ = await self.tap(101, self.buttons()["Older products"])
        self.assertEqual(self.catalog_ids(texts), ids[:1])
        texts, _ = await self.tap(100, "menu:myproducts")
        self.assertIn("Your product #21", texts[0])
        self.assertIn(":mine:all:", self.buttons()["Older products"])

    async def test_myproducts_keeps_numbers_and_uses_the_requesting_seller(self):
        other = self.seed_products(seller=200, count=2)
        mine = self.seed_products(count=23)
        self.store.remove_seller_product(100, mine[0])
        texts = await self.say(100, "/myproducts")
        self.assertIn(f"Your product #23 (catalog ID #{mine[-1]})", texts[0])
        older = self.buttons()["Older products"]
        texts, _ = await self.tap(100, older)
        self.assertIn(f"Your product #3 (catalog ID #{mine[2]})", texts[0])
        self.assertIn(f"Your product #2 (catalog ID #{mine[1]})", texts[0])
        self.assertNotIn("Your product #1 ", "\n".join(texts))
        self.assertNotIn("Item 200-", "\n".join(texts))
        # A copied button cannot request the original seller's listings.
        texts, _ = await self.tap(200, older)
        self.assertIn(f"catalog ID #{other[-1]}", texts[0])
        self.assertNotIn("Item 100-", "\n".join(texts))
        texts, _ = await self.tap(300, "prodpage:mine:all:new")
        self.assertEqual(texts, ["You have no products yet. Use /addproduct."])

    async def test_removed_remaining_products_offer_newest(self):
        ids = self.seed_products(count=21)
        await self.say(101, "/products all")
        older = self.buttons()["Older products"]
        self.store.remove_seller_product(100, ids[0])
        texts, answers = await self.tap(101, older)
        self.assertEqual(len(texts), 1)
        self.assertIn("No older products remain", texts[0])
        self.assertEqual(len(answers), 1)
        self.assertEqual(set(self.buttons()), {"Newest products"})

    async def test_buttons_survive_application_restart(self):
        ids = self.seed_products(count=21)
        await self.say(101, "/products all")
        older = self.buttons()["Older products"]
        self.app = await self.make_app()
        texts, answers = await self.tap(101, older)
        self.assertEqual(self.catalog_ids(texts), ids[:1])
        self.assertEqual(len(answers), 1)

    async def test_invalid_callbacks_never_query_the_store(self):
        invalid = ("prodpage:", "prodpage:all:all:0", "prodpage:all:all:-1",
                   "prodpage:all:all:9223372036854775808", "prodpage:all:all:" + "9" * 20,
                   "prodpage:all:all:1:100", "prodpage:mine:art:new", "prodpage:100:all:new",
                   "prodpage:all:unknown:new", "prodpage:all:all:1.5",
                   "prodpage:all:all:" + "0" * 65, "prodpage:all:all:１")
        with patch.object(self.store, "list_products_page") as fetch:
            for data in invalid:
                with self.subTest(data=data):
                    texts, answers = await self.tap(101, data)
                    self.assertEqual(len(answers), 1)
                    self.assertEqual(len(texts), 1)
                    self.assertIn("Invalid product page", texts[0])
            fetch.assert_not_called()

    async def test_cursor_range_endpoints_are_accepted(self):
        ids = self.seed_products(count=1)
        texts, _ = await self.tap(101, "prodpage:all:all:9223372036854775807")
        self.assertEqual(self.catalog_ids(texts), ids)
        texts, _ = await self.tap(101, "prodpage:all:all:1")
        self.assertIn("No older products remain", texts[0])

    async def test_group_callbacks_are_acknowledged_without_query_or_results(self):
        with patch.object(self.store, "list_products_page") as fetch:
            start = len(self.sent)
            texts = await self.feed(self._update(callback_query={
                "id": "group", "chat_instance": "x", "data": "prodpage:all:all:new",
                "from": {"id": 101, "is_bot": False, "first_name": "T"},
                "message": self._message(101, chat_type="group", text="old page")}))
            self.assertEqual(texts, [])
            answers = [p for api, p in self.sent[start:] if api == "answerCallbackQuery"]
            self.assertEqual(len(answers), 1)
            self.assertIn("private chat", answers[0]["text"])
            fetch.assert_not_called()

    async def test_navigation_preserves_registration_and_product_entry(self):
        self.seed_products(count=1)
        await self.begin(101)
        await self.say(101, NAME_EMAIL)
        before = dict(self.app.user_data[101])
        texts, answers = await self.tap(101, "prodpage:all:all:new")
        self.assertEqual(len(texts), 2)  # one listing message plus guidance
        self.assertEqual(len(answers), 1)
        self.assertEqual(dict(self.app.user_data[101]), before)
        await self.say(101, PHONE)
        self.assertEqual(self.store.get_user(101)["full_name"], "Jane Doe")
        await self.say(100, "/addproduct")
        await self.tap(100, "prodpage:mine:all:new")
        await self.say(100, PRODUCT)
        self.assertEqual(self.store.count_products(100), 2)
        pending = self.app.user_data[100]["pending_product_id"]
        await self.tap(100, "prodpage:all:all:new")
        self.assertEqual(self.app.user_data[100]["pending_product_id"], pending)
        texts = await self.say(100, "/skip")
        self.assertIn("Done", texts[0])

    async def test_database_failure_is_reported_once_and_preserves_conversation(self):
        await self.begin(101)
        with patch.object(self.store, "list_products_page", side_effect=RuntimeError("offline")):
            with self.assertLogs("handlers", level="ERROR"):
                texts, answers = await self.tap(101, "prodpage:all:all:new")
            self.assertEqual(len(answers), 1)
            self.assertEqual(texts, ["The service is unavailable. Please try again later."])
        await self.say(101, NAME_EMAIL)
        await self.say(101, PHONE)
        self.assertIsNotNone(self.store.get_user(101))

    async def test_send_failure_is_logged_without_falling_through_to_a_conversation_hint(self):
        self.seed_products(count=1)
        await self.begin(101)
        original = HTTPXRequest.do_request
        failed = False

        async def fail_once(request, url, *args, **kwargs):
            nonlocal failed
            if url.endswith("/sendMessage") and not failed:
                failed = True
                raise NetworkError("simulated send failure")
            return await original(request, url, *args, **kwargs)

        with patch.object(HTTPXRequest, "do_request", fail_once):
            with self.assertLogs("handlers", level="ERROR"):
                texts, answers = await self.tap(101, "prodpage:all:all:new")
        self.assertTrue(failed)
        self.assertEqual(len(answers), 1)
        self.assertEqual(texts, ["The service is unavailable. Please try again later."])
