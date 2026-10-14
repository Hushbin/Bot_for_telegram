import unittest
from decimal import Decimal

from catalog import CATEGORIES, LEGACY_CATEGORY_LABELS, category_label
from handlers import parse_product, product_text, stored_data_text


class CatalogTests(unittest.TestCase):
    def test_every_legacy_label_maps_to_a_category_that_still_exists(self):
        self.assertTrue(set(LEGACY_CATEGORY_LABELS.values()) <= set(CATEGORIES))

    def test_the_legacy_table_is_the_frozen_set_of_original_labels(self):
        # If this fails because a label was reworded in CATEGORIES, leave the legacy
        # table alone: it describes data written before keys existed.
        self.assertEqual(LEGACY_CATEGORY_LABELS, {
            "Clothing/Fashion": "clothing", "Hardware Accessories": "hardware",
            "Food/Kitchen Ware": "food", "Art & Design": "art"})

    def test_labels_are_for_display_and_unknown_values_pass_through(self):
        self.assertEqual(category_label("art"), "Art & Design")
        self.assertEqual(category_label("Gadgets"), "Gadgets")

    def test_new_products_carry_the_key_never_the_label(self):
        for typed in ("art", "ART", "Art"):
            with self.subTest(typed=typed):
                self.assertEqual(parse_product(f"Hat | {typed} | 1 | 1 | x")[1], "art")
        with self.assertRaises(ValueError):
            parse_product("Hat | Art & Design | 1 | 1 | x")

    def test_price_is_parsed_into_minor_units(self):
        self.assertEqual(parse_product("Hat | art | 19.99 | 1 | x")[3], 1999)
        self.assertEqual(parse_product("Hat | art | 0 | 1 | x")[3], 0)

    def test_renaming_a_label_changes_the_display_but_not_what_is_stored(self):
        product = dict(product_id=1, seller_number=1, name="Hat", category="art",
                       description=None, price_minor=1999, currency="USD",
                       stock_quantity=2, business_name="Shop")
        self.assertIn("Art & Design", product_text(product))
        CATEGORIES["art"] = "Arts and Crafts"
        self.addCleanup(CATEGORIES.__setitem__, "art", "Art & Design")
        self.assertIn("Arts and Crafts", product_text(product))
        self.assertEqual(product["category"], "art")

    def test_product_text_shows_the_rows_currency_and_amount(self):
        product = dict(product_id=1, seller_number=1, name="Hat", category="art",
                       description=None, price_minor=1999, currency="USD",
                       stock_quantity=2, business_name="Shop")
        self.assertIn("Price: USD 19.99", product_text(product))
        self.assertIn("Price: EUR 5.05", product_text(dict(product, price_minor=505,
                                                           currency="EUR")))
        legacy_row = {k: v for k, v in product.items() if k != "currency"}
        self.assertIn("Price: USD 19.99", product_text(legacy_row))
        self.assertEqual(Decimal(1999) / 100, Decimal("19.99"))

    def test_my_data_shows_the_category_label(self):
        user = dict(telegram_user_id=1, full_name="J", email="j@example.com", phone="+1",
                    phone_verified=False, role="CUSTOMER", preferred_category="food")
        self.assertIn("Preferred category: Food/Kitchen Ware", stored_data_text(user, None, 0))
        self.assertIn("Preferred category: none",
                      stored_data_text(dict(user, preferred_category=None), None, 0))


if __name__ == "__main__":
    unittest.main()
