"""Product categories and currency, shared by the handlers and the database migrations.

Rows store the category *key* (``clothing``), never the display label, so a label can be
reworded here without orphaning stored rows.
"""

CATEGORIES = {
    "clothing": "Clothing/Fashion",
    "hardware": "Hardware Accessories",
    "food": "Food/Kitchen Ware",
    "art": "Art & Design",
}

# The labels that category columns held before keys were introduced, mapped to their
# key. Used once by the SQLite and Oracle migrations. Frozen on purpose: if a label in
# CATEGORIES is reworded later, leave this table as it was.
LEGACY_CATEGORY_LABELS = {
    "Clothing/Fashion": "clothing",
    "Hardware Accessories": "hardware",
    "Food/Kitchen Ware": "food",
    "Art & Design": "art",
}

DEFAULT_CURRENCY = "USD"
# Prices are stored in minor units (cents). Every currency the bot may offer must have
# two decimal places; add an exponent per currency before offering one that does not.
MINOR_UNITS_PER_MAJOR = 100

PRODUCT_PAGE_SIZE = 20
# Use the ID range shared by SQLite and Oracle, as with order IDs.
MAX_PRODUCT_ID = 2**63 - 1


def validate_product_cursor(before_id):
    if before_id is not None and (
        type(before_id) is not int or not 0 < before_id <= MAX_PRODUCT_ID
    ):
        raise ValueError("Invalid product page cursor.")


def category_label(key):
    """Display text for a stored category key; an unknown value is shown as stored."""
    return CATEGORIES.get(key, key)
