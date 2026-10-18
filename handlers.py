"""Telegram conversations backed by Oracle and private OCI Object Storage."""

import asyncio
import io
import logging
import re
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import uuid4

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import ApplicationHandlerStop, ConversationHandler

from catalog import (
    CATEGORIES, DEFAULT_CURRENCY, MINOR_UNITS_PER_MAJOR, MAX_PRODUCT_ID, category_label,
)
from photo_delivery import ProductPhotoDelivery


LOG = logging.getLogger(__name__)
(CHOOSING, CLASS_STATE, SME_DETAILS, CHOOSE_PREF, PRODUCT_DETAILS, PRODUCT_PHOTO,
 PHONE, CONFIRM_ERASE, PRODUCT_ID, PHOTO_ID, STOCK_INPUT, BROWSE_CATEGORY) = range(12)
(PRODUCT_CATEGORY, PRODUCT_PRICE, PRODUCT_STOCK, PRODUCT_DESCRIPTION,
 PRODUCT_REVIEW) = range(30, 35)
PRODUCT_FIELDS = ("name", "category", "price_minor", "stock_quantity", "description")
PRODUCT_FIELD_STATES = dict(zip(PRODUCT_FIELDS, (
    PRODUCT_DETAILS, PRODUCT_CATEGORY, PRODUCT_PRICE, PRODUCT_STOCK, PRODUCT_DESCRIPTION)))
MAX_PHOTO_BYTES = 5 * 1024 * 1024

PRIVACY_NOTICE = (
    "What I store about you:\n"
    "- buyers: your Telegram ID, name, email, phone number (and whether Telegram "
    "verified it), role and optional category preference;\n"
    "- sellers: account details, listing name, location, seller contact details, "
    "and products with photos;\n"
    "- orders: buyer and seller IDs, product and listing names, quantity, confirmed "
    "price, buyer name and phone snapshot, consent version, status and notification delivery.\n\n"
    "Who can see it: the bot operator sees stored data. Product listings are public. "
    "When you confirm an order, your name and phone are shared with that seller, "
    "as shown on the confirmation screen. Email is not shared. Order details are "
    "visible only to the participating buyer and seller. Seller contact details "
    "are not automatically shared.\n\n"
    "How long: live profiles and order contact snapshots stay until you erase them. "
    "Daily recovery exports are kept for seven days (OCI deletion may take another day); "
    "managed database backups can last up to 60 days.\n\n"
    "Your controls: /mydata shows stored data; /orders and /sales show order details. "
    "/deleteme erases your personal data, listing and photos, scrubs your order "
    "snapshots and cancels open orders. Redacted order IDs, numeric participant IDs, "
    "quantities, prices, statuses and timestamps remain for stock and history consistency. "
    "A restore honors erasure records. Copies already delivered to another person's "
    "Telegram chat, including a send during erasure, cannot reliably be recalled."
)
SELLER_NOTICE = (
    "Your listing name and products are visible to other users. Your location, seller email and "
    "phone are not automatically shared. Confirming a purchase shares your buyer "
    "name and phone with that seller. Incoming orders show the buyer's confirmed "
    "name and phone; use them only to arrange that order. If you use your own "
    "name as the listing name, that name will be public."
)
BUSINESS_PROMPT = (
    "Choose a listing name: a shop name, nickname or your own name. You do not need "
    "a registered business. Send: listing name | city/area (or Online)\n"
    "Example: Jane's finds | Belgrade\n"
    "I'll reuse your account email and phone for seller contact. To use different "
    "seller contact details, add: | email | phone."
)

EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
PHONE_PATTERN = re.compile(r"\+?[0-9 ()-]{7,40}")
CATALOG_ID_PROMPT = (
    "Send a catalog ID from /products or /myproducts, for example 1 or #1."
)


def clean_name(value):
    value = value.strip()
    if not 1 <= len(value) <= 160:
        raise ValueError("Enter a name of 1 to 160 characters.")
    return value


def clean_email(value):
    value = value.strip()
    if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
        raise ValueError("Enter a valid email address.")
    return value


def clean_phone(value):
    value = value.strip()
    if not PHONE_PATTERN.fullmatch(value):
        raise ValueError("Enter a valid phone number, for example +1234567890.")
    return value


def parse_catalog_id(value):
    raw = value.strip().removeprefix("#")
    if not re.fullmatch(r"[0-9]{1,19}", raw) or int(raw) <= 0:
        raise ValueError("Enter a positive catalog ID, such as 1 or #1.")
    return int(raw)


def parse_name_email(text):
    # Split on the last comma so a name such as "Doe, Jane" still works.
    name, separator, email = text.rpartition(",")
    if not separator:
        raise ValueError("Send your full name and email separated by a comma.")
    return clean_name(name), clean_email(email)


def parse_telegram_phone(raw):
    """Normalise a number from Telegram's contact card, which may omit the '+'."""
    raw = raw.strip()
    if raw.isdigit():
        raw = "+" + raw
    return clean_phone(raw)


def parse_business(text, account_email=None, account_phone=None):
    if "|" in text:
        fields = [part.strip() for part in text.split("|")]
        if len(fields) not in (2, 4):
            raise ValueError("Send listing name | city/area, optionally followed by | email | phone.")
        name, address = fields[:2]
        if not address or len(address) > 400:
            raise ValueError("Enter a city/area or Online (up to 400 characters).")
        email = fields[2] if len(fields) == 4 and fields[2] else account_email
        phone = fields[3] if len(fields) == 4 and fields[3] else account_phone
        if not email or not phone:
            raise ValueError("Account email and phone are missing. Update your account with /profile first.")
        return clean_name(name), clean_email(email), address, clean_phone(phone)
    # Accept the earlier four-field format when a user edits an existing message.
    fields = [part.strip() for part in text.split(",")]
    if len(fields) < 4:
        raise ValueError("Send listing name | city/area, for example Jane's finds | Belgrade.")
    # The address may itself contain commas, so it is everything between email and phone.
    name, email, phone = fields[0], fields[1], fields[-1]
    address = ", ".join(fields[2:-1])
    if not address or len(address) > 400:
        raise ValueError("Enter a location of at most 400 characters.")
    return clean_name(name), clean_email(email), address, clean_phone(phone)


def clean_product_category(value):
    key = value.strip().lower()
    if key not in CATEGORIES:
        raise ValueError("Choose a product category from the buttons.")
    return key


def clean_product_price(value):
    try:
        price = Decimal(value.strip())
        if (not price.is_finite() or not 0 <= price < 10**10
                or price != price.quantize(Decimal("0.01"))):
            raise ValueError
        return int(price * MINOR_UNITS_PER_MAJOR)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError("Enter a nonnegative USD price with at most two decimals, "
                         "up to 9999999999.99 (for example 19.99).") from exc


def clean_product_stock(value):
    try:
        quantity = int(value)
        if not 0 <= quantity < 10**10:
            raise ValueError
        return quantity
    except ValueError as exc:
        raise ValueError("Enter whole-number stock from 0 to 9999999999, for example 5.") from exc


def clean_product_description(value):
    value = value.strip()
    if len(value) > 1000:
        raise ValueError("Enter a description of at most 1000 characters, or /skip.")
    return value or None


def parse_product(text):
    fields = [part.strip() for part in text.split("|", 4)]
    if len(fields) != 5:
        raise ValueError("Use: name | category | price | stock | description")
    name, category, price, stock, description = fields
    return (clean_name(name), clean_product_category(category),
            clean_product_description(description), clean_product_price(price), clean_product_stock(stock))


def product_text(product, *, detail=False, seller_view=False):
    amount = Decimal(product["price_minor"]) / MINOR_UNITS_PER_MAJOR
    currency = product.get("currency") or DEFAULT_CURRENCY
    description = product["description"] or ""
    description = description[:400 if detail else 140]
    identifier = (f'Your product #{product["seller_number"]} '
                  f'(catalog ID #{product["product_id"]})' if seller_view
                  else f'#{product["product_id"]}')
    return (
        f'{identifier} {product["name"]}\n'
        f'{product["business_name"]} · {category_label(product["category"])}\n'
        f'Price: {currency} {amount:.2f} · In stock: {product["stock_quantity"]}\n'
        f'{description}'
    ).strip()


def stored_data_text(user, seller, product_count):
    phone_source = "verified by Telegram" if user["phone_verified"] else "typed by you"
    lines = [
        "Data stored about you:",
        f'Telegram ID: {user["telegram_user_id"]}',
        f'Name: {user["full_name"]}',
        f'Email: {user["email"]}',
        f'Phone: {user["phone"]} ({phone_source})',
        f'Role: {"seller" if user["role"] == "SELLER" else "buyer"}',
        f'Preferred category: {category_label(user["preferred_category"]) if user["preferred_category"] else "none"}',
    ]
    if seller:
        lines += [
            "",
            "Seller profile:",
            f'Listing name: {seller["business_name"]}',
            f'Seller contact email: {seller["business_email"]}',
            f'Location: {seller["address"]}',
            f'Seller contact phone: {seller["business_phone"]}',
            f"Products listed: {product_count}",
        ]
    lines += ["", "Use /deleteme to erase it."]
    return "\n".join(lines)


async def on_error(update, context):
    """Last-resort handler: log the failure and tell the user instead of going silent."""
    update_id = getattr(update, "update_id", None)
    LOG.error("Unhandled error while processing update_id=%s", update_id,
              exc_info=context.error)
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text("Something went wrong. Please try again.")
    except Exception:
        LOG.exception("Could not send the error notice for update_id=%s", update_id)


def category_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"cat:{key}")]
        for key, name in CATEGORIES.items()
    ])


def phone_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Share my Telegram phone number", request_contact=True)]],
        resize_keyboard=True, is_persistent=True, one_time_keyboard=False,
        input_field_placeholder="Share your number or type it",
    )


def role_buttons():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Seller", callback_data="role:seller"),
        InlineKeyboardButton("Buyer", callback_data="role:buyer"),
    ]])


def menu_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Browse products", callback_data="menu:products"),
         InlineKeyboardButton("View product", callback_data="menu:product")],
        [InlineKeyboardButton("My profile", callback_data="menu:profile"),
         InlineKeyboardButton("My data", callback_data="menu:mydata")],
        [InlineKeyboardButton("Seller profile", callback_data="menu:sellerprofile"),
         InlineKeyboardButton("My products", callback_data="menu:myproducts")],
        [InlineKeyboardButton("Add product", callback_data="menu:addproduct"),
         InlineKeyboardButton("Add photo", callback_data="menu:photo")],
        [InlineKeyboardButton("Update stock", callback_data="menu:stock"),
         InlineKeyboardButton("Categories", callback_data="menu:preferences")],
        [InlineKeyboardButton("My orders", callback_data="menu:orders"),
         InlineKeyboardButton("Incoming orders", callback_data="menu:sales")],
        [InlineKeyboardButton("Privacy", callback_data="menu:privacy"),
         InlineKeyboardButton("Erase my data", callback_data="menu:deleteme")],
        [InlineKeyboardButton("Start / welcome", callback_data="menu:start")],
    ])


def browse_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("All products", callback_data="browse:all")],
        *[[InlineKeyboardButton(name, callback_data=f"browse:{key}")]
          for key, name in CATEGORIES.items()],
    ])


class BotHandlers:
    def __init__(self, store, media, erasures=None, orders_enabled=False):
        self.store = store
        self.media = media
        self.erasures = erasures
        from order_handlers import OrderHandlers
        self.order_handlers = OrderHandlers(self, orders_enabled)

    async def run(self, operation, *args, **kwargs):
        return await asyncio.to_thread(operation, *args, **kwargs)

    async def owned_product(self, seller_id, reference):
        """Seller numbers are local; G-prefixed IDs address the shared catalog."""
        raw = reference.strip().lower()
        catalog = raw.startswith("g")
        digits = raw[1:] if catalog else raw
        if not re.fullmatch(r"[0-9]{1,19}", digits) or int(digits) <= 0:
            raise ValueError("Use your product number, or G followed by a catalog ID.")
        number = int(digits)
        if catalog:
            product = await self.run(self.store.get_product, number)
        else:
            product = await self.run(self.store.get_seller_product, seller_id, number)
        if product and product["seller_telegram_user_id"] == seller_id:
            return product
        return None

    @staticmethod
    def reset_flow(context):
        """Forget the half-finished input of whatever flow the user was in."""
        for key in ("last_input", "draft", "pending_product_id",
                    "pending_product_number", "signup_role",
                    "phone_keyboard_active", "order_after_signup", "order_product_id", "order_draft",
                    "product_draft"):
            context.user_data.pop(key, None)

    @staticmethod
    async def hide_phone_keyboard(update, context):
        if context.user_data.pop("phone_keyboard_active", False):
            await update.effective_message.reply_text(
                "Phone input closed.", reply_markup=ReplyKeyboardRemove()
            )

    @staticmethod
    def remember(context, message, step, **extra):
        """Record the user's latest text input so an edit of it can be applied."""
        context.user_data["last_input"] = {"id": message.message_id, "step": step, **extra}

    async def not_private(self, update, context):
        await update.effective_message.reply_text("Please message me in a private chat.")

    @staticmethod
    def waiting_for(prompt):
        """Build a catch-all for a conversation state: say what the bot expects.

        Returns None so the conversation stays in its current state.
        """
        async def hint(update, context):
            if update.callback_query:
                await update.callback_query.answer()
            await update.effective_message.reply_text(f"{prompt} Or send /cancel to stop.")
        return hint

    async def expired_button(self, update, context):
        """Answer inline buttons that no active conversation owns (e.g. after a restart)."""
        query = update.callback_query
        await query.answer("This menu has expired. Send /start to continue.", show_alert=True)

    async def failure(self, update):
        LOG.exception("Bot operation failed")
        await update.effective_message.reply_text("The service is unavailable. Please try again later.")

    async def edited(self, update, context):
        """Treat an edit of the user's latest text input as a correction of it.

        Edits of any other message are ignored. The step handler is run again with the
        edited text and decides the next state, so an edit of a message the bot rejected
        moves the flow on, while an edit of an accepted one updates what was saved.
        """
        message = update.effective_message
        last = context.user_data.get("last_input")
        if not last or last["id"] != message.message_id:
            return None
        step = {
            "order_quantity": self.order_handlers.quantity,
            "contact": self.choose,
            "phone": self.phone_text,
            "business": self.seller_details,
            "product": self.product_details,
            "product_entry": self.product_entry_text,
        }.get(last["step"])
        return await step(update, context) if step else None

    async def privacy(self, update, context):
        await update.effective_message.reply_text(PRIVACY_NOTICE)

    async def my_data(self, update, context):
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
            seller = products = None
            if user:
                seller = await self.run(self.store.get_seller, update.effective_user.id)
                products = await self.run(self.store.count_products, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return
        if not user:
            await update.effective_message.reply_text(
                "I have no data stored about you. Use /start to register."
            )
            return
        await update.effective_message.reply_text(stored_data_text(user, seller, products))
        await update.effective_message.reply_text(
            "Order snapshots and consent records: use /orders for your purchases and "
            "/sales for incoming orders, then open each order. Your confirmed name and "
            "phone, price, quantity and status are retained until account erasure; "
            "redacted commercial history remains afterward.")

    async def delete_me(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not user:
            await update.effective_message.reply_text("I have no data stored about you.")
            return ConversationHandler.END
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, erase everything", callback_data="erase:yes"),
            InlineKeyboardButton("No, keep my data", callback_data="erase:no"),
        ]])
        await update.effective_message.reply_text(
            "This removes your name, email, phone, seller profile, products and "
            "photos from the live service. Recovery exports expire after seven days "
            "(OCI deletion may take another day); managed database backups can last "
            "up to 60 days. A restore will honor this erasure. Open orders are cancelled "
            "and your order contacts are scrubbed. Redacted order history remains; "
            "copies delivered in Telegram cannot reliably be recalled. Continue?",
            reply_markup=buttons,
        )
        return CONFIRM_ERASE

    async def confirm_erase(self, update, context):
        query = update.callback_query
        await query.answer()
        if query.data != "erase:yes":
            await query.message.reply_text("Nothing was erased.")
            return ConversationHandler.END
        if self.erasures is not None:
            try:
                await self.run(self.erasures.record_erasure, update.effective_user.id)
            except Exception:
                LOG.exception("Could not record durable erasure marker")
                await query.message.reply_text(
                    "Erasure could not start because recovery storage is unavailable. "
                    "Please try again later."
                )
                return CONFIRM_ERASE
        try:
            photos = await self.run(self.store.erase_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return CONFIRM_ERASE
        failed = 0
        for key, version_id in photos:
            try:
                await self.run(self.media.delete_photo, key, version_id)
            except Exception:
                failed += 1
                LOG.exception("Could not delete photo object, remove it manually: %s", key)
        context.user_data.clear()
        text = "Your data has been erased. Send /start if you want to register again."
        if failed:
            text += (f"\n{failed} product photo(s) could not be removed from storage yet; "
                     "the operator has been notified through the logs.")
        await query.message.reply_text(text)
        return ConversationHandler.END

    async def start(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if user:
            await update.effective_message.reply_text(
                f'Welcome back, {user["full_name"]}!\n'
                "Choose an action below or send /menu anytime.",
                reply_markup=menu_buttons(),
            )
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Welcome to SMEbot! Are you a seller or a buyer?",
            reply_markup=role_buttons(),
        )
        return CLASS_STATE

    async def menu(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        await update.effective_message.reply_text(
            "Choose an action. You can send /menu again anytime.",
            reply_markup=menu_buttons(),
        )
        return ConversationHandler.END

    async def menu_action(self, update, context):
        query = update.callback_query
        if update.effective_chat.type != "private":
            await query.answer("Please message me in a private chat.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        action = query.data.removeprefix("menu:")
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        if action == "products":
            await query.message.reply_text(
                "Which products would you like to browse?", reply_markup=browse_buttons()
            )
            return BROWSE_CATEGORY
        if action == "product":
            await query.message.reply_text(CATALOG_ID_PROMPT)
            return PRODUCT_ID
        if action == "photo":
            await query.message.reply_text(
                "Send your product number to add a photo (or G plus its catalog ID)."
            )
            return PHOTO_ID
        if action == "stock":
            await query.message.reply_text(
                "Send your product number and new stock, for example: 1 5. "
                "Use G3 5 for catalog ID #3."
            )
            return STOCK_INPUT
        actions = {
            "orders": self.order_handlers.purchases,
            "sales": self.order_handlers.sales,
            "start": self.start,
            "profile": self.profile,
            "sellerprofile": self.seller_profile,
            "preferences": self.preferences,
            "addproduct": self.add_product,
            "myproducts": self.my_products,
            "privacy": self.privacy,
            "mydata": self.my_data,
            "deleteme": self.delete_me,
        }
        callback = actions.get(action)
        if callback is None:
            await query.message.reply_text("That menu item is unavailable. Send /menu again.")
            return ConversationHandler.END
        result = await callback(update, context)
        return result if result is not None else ConversationHandler.END

    async def browse_selection(self, update, context):
        query = update.callback_query
        if update.effective_chat.type != "private":
            await query.answer("Please message me in a private chat.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        key = query.data.removeprefix("browse:")
        if key not in CATEGORIES and key != "all":
            return BROWSE_CATEGORY
        context.args = [key]
        await self.products(update, context)
        return ConversationHandler.END

    async def product_id(self, update, context):
        raw = update.effective_message.text.strip()
        try:
            parse_catalog_id(raw)
        except ValueError:
            await update.effective_message.reply_text(
                "Send a positive catalog ID, such as 1 or #1, or /cancel."
            )
            return PRODUCT_ID
        context.args = [raw]
        await self.product(update, context)
        return ConversationHandler.END

    async def photo_id(self, update, context):
        raw = update.effective_message.text.strip()
        if not re.fullmatch(r"(?:[gG])?[0-9]{1,19}", raw) or int(raw.lstrip("gG")) <= 0:
            await update.effective_message.reply_text(
                "Send your product number, or G plus its catalog ID. Use /cancel to stop."
            )
            return PHOTO_ID
        context.args = [raw]
        return await self.add_photo(update, context)

    async def stock_input(self, update, context):
        fields = update.effective_message.text.split()
        if (len(fields) != 2
                or not re.fullmatch(r"(?:[gG])?[0-9]{1,19}", fields[0])
                or not re.fullmatch(r"[0-9]{1,19}", fields[1])):
            await update.effective_message.reply_text(
                "Send your product number and quantity, for example: 1 5"
            )
            return STOCK_INPUT
        context.args = fields
        await self.stock(update, context)
        return ConversationHandler.END

    async def profile(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not user:
            await update.effective_message.reply_text("Register with /start first.")
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Update your account contact details. Send your full name and email "
            "separated by a comma. Example: Jane Doe, jane@example.com. "
            "The operator sees these details. A seller receives your name and phone "
            "only when you confirm an order. Existing orders keep their confirmed contact snapshot. "
            "You can update your seller listing separately with /sellerprofile."
        )
        return CHOOSING

    async def seller_profile(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not user:
            await update.effective_message.reply_text("Register your personal details with /start first.")
            return ConversationHandler.END
        await update.effective_message.reply_text(f"{BUSINESS_PROMPT}\n\n{SELLER_NOTICE}")
        return SME_DETAILS

    async def preferences(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not user:
            await update.effective_message.reply_text("Register with /start first.")
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Choose a category you like:", reply_markup=category_buttons()
        )
        return CHOOSE_PREF

    async def choose(self, update, context):
        """First registration step: name and email. The phone number comes next."""
        message = update.effective_message
        self.remember(context, message, "contact")
        try:
            name, email = parse_name_email(message.text)
        except ValueError as exc:
            await message.reply_text(f"{exc} Try again or /cancel.")
            return CHOOSING
        context.user_data["draft"] = {"name": name, "email": email}
        context.user_data["phone_keyboard_active"] = True
        await message.reply_text(
            f"Thanks, {name}. Now your account phone number. The bot operator can see it; "
            "a seller also receives it if you later confirm an order. Tap the button to share the number "
            "verified by Telegram, or type a different number, for example +1234567890. "
            "If you see the typing keyboard instead, tap the keyboard icon beside the "
            "message field to show the share button.",
            reply_markup=phone_keyboard(),
        )
        return PHONE

    async def phone_contact(self, update, context):
        message = update.effective_message
        self.remember(context, message, "phone_contact")
        contact = message.contact
        if contact.user_id != update.effective_user.id:
            await message.reply_text(
                "That contact is not your own number. Tap the button to share yours, "
                "or type a number."
            )
            return PHONE
        try:
            phone = parse_telegram_phone(contact.phone_number)
        except ValueError as exc:
            await message.reply_text(f"{exc} Type a number instead.")
            return PHONE
        return await self.save_contact(update, context, phone, verified=True)

    async def phone_text(self, update, context):
        message = update.effective_message
        self.remember(context, message, "phone")
        try:
            phone = clean_phone(message.text)
        except ValueError as exc:
            await message.reply_text(f"{exc} Try again or /cancel.")
            return PHONE
        return await self.save_contact(update, context, phone, verified=False)

    async def save_contact(self, update, context, phone, *, verified):
        message = update.effective_message
        draft = context.user_data.get("draft")
        if not draft:
            context.user_data.pop("phone_keyboard_active", None)
            await message.reply_text("Let's start again: send /start.",
                                     reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        try:
            role = context.user_data.get("signup_role")
            user = await self.run(self.store.upsert_user, update.effective_user.id,
                                  draft["name"], draft["email"], phone, verified, role)
        except Exception:
            await self.failure(update)
            return PHONE
        await message.reply_text(
            "Phone number saved" + (" (verified by Telegram)." if verified else "."),
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.pop("phone_keyboard_active", None)
        if role == "SELLER":
            await message.reply_text(
                f"Thanks, {draft['name']}. Now set up your seller listing.\n\n"
                f"{BUSINESS_PROMPT}\n\n{SELLER_NOTICE}"
            )
            return SME_DETAILS
        if role == "CUSTOMER":
            if context.user_data.get("order_after_signup") is not None:
                return await self.order_handlers.resume_signup(update, context)
            await message.reply_text(
                f"Thanks, {draft['name']}. Your buyer profile is ready. "
                "Use /menu to explore the bot.", reply_markup=menu_buttons(),
            )
            return ConversationHandler.END
        await message.reply_text(
            "Profile updated. Choose an action or send /menu.",
            reply_markup=menu_buttons(),
        )
        return ConversationHandler.END

    async def classer(self, update, context):
        query = update.callback_query
        await query.answer()
        role = {"role:seller": "SELLER", "role:buyer": "CUSTOMER",
                "role:customer": "CUSTOMER"}.get(query.data)
        if role is None:
            return CLASS_STATE
        order_intent = context.user_data.get("order_after_signup")
        self.reset_flow(context)
        if order_intent is not None:
            context.user_data["order_after_signup"] = order_intent
        context.user_data["signup_role"] = role
        await query.message.reply_text(
            ("Seller" if role == "SELLER" else "Buyer") + " privacy notice:\n\n" + PRIVACY_NOTICE
        )
        await query.message.reply_text(
            "First, your account contact details. Send your full name and email "
            "separated by a comma. The operator sees these details; a seller receives "
            "your name and phone only when you confirm an order.\nExample: Jane Doe, jane@example.com\n"
            "Use /cancel to stop."
        )
        return CHOOSING

    async def seller_details(self, update, context):
        message = update.effective_message
        corrected = getattr(update, "edited_message", None) is not None
        self.remember(context, message, "business")
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return SME_DETAILS
        if not user:
            await message.reply_text("Register your account contact details with /start first.")
            return ConversationHandler.END
        try:
            business = parse_business(message.text, user["email"], user["phone"])
        except ValueError as exc:
            await message.reply_text(f"{exc} Try again or /cancel.")
            return SME_DETAILS
        try:
            await self.run(self.store.upsert_seller, update.effective_user.id, *business)
        except Exception:
            await self.failure(update)
            return SME_DETAILS
        await message.reply_text(
            "Seller profile updated." if corrected
            else "Seller profile saved. Use /menu to list your first product.",
            reply_markup=menu_buttons() if not corrected else None,
        )
        if context.user_data.get("order_after_signup") is not None:
            return await self.order_handlers.resume_signup(update, context)
        return ConversationHandler.END

    async def preference(self, update, context):
        query = update.callback_query
        await query.answer()
        key = query.data.removeprefix("cat:")
        if key not in CATEGORIES:
            return CHOOSE_PREF
        try:
            await self.run(self.store.set_preference, update.effective_user.id, key)
        except Exception:
            await self.failure(update)
            return CHOOSE_PREF
        self.reset_flow(context)
        await query.message.reply_text(
            f"Saved {category_label(key)}. Use /products to see available products."
        )
        return ConversationHandler.END

    async def add_product(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        try:
            seller = await self.run(self.store.get_seller, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not seller:
            await update.effective_message.reply_text("Register a seller profile with /sellerprofile first.")
            return ConversationHandler.END
        context.user_data["product_draft"] = {"field": "name", "fields": {}}
        await update.effective_message.reply_text(
            "Send a product name to enter details step by step.\n"
            "Or use the shortcut: name | category | price in USD | stock | description\n"
            "Category keys: clothing, hardware, food, art.\n"
            "Example: Blue shirt | clothing | 19.99 | 5 | Cotton shirt"
        )
        return PRODUCT_DETAILS

    async def product_details(self, update, context):
        message = update.effective_message
        last = context.user_data.get("last_input") or {}
        if "|" not in message.text and not (
            getattr(update, "edited_message", None) is not None and last.get("step") == "product"
        ):
            return await self.product_entry_text(update, context)
        # An edit of the line that already created a product updates that product.
        product_id = None
        seller_number = None
        if (getattr(update, "edited_message", None) is not None
                and last.get("step") == "product" and last.get("id") == message.message_id):
            product_id = last.get("product_id")
            seller_number = last.get("seller_number")
        retry_state = PRODUCT_PHOTO if product_id else PRODUCT_DETAILS
        self.remember(context, message, "product", **(
            {"product_id": product_id, "seller_number": seller_number} if product_id else {}
        ))
        try:
            fields = parse_product(message.text)
        except ValueError as exc:
            await message.reply_text(f"{exc} Try again or /cancel.")
            return retry_state
        if product_id:
            try:
                changed = await self.run(self.store.update_product,
                                         update.effective_user.id, product_id, *fields)
            except Exception:
                await self.failure(update)
                return retry_state
            await message.reply_text(
                f"Your product #{seller_number} updated." if changed and seller_number
                else f"Product #{product_id} updated." if changed
                else "That product no longer exists."
            )
            return PRODUCT_PHOTO if changed else ConversationHandler.END
        try:
            product_id, seller_number = await self.run(
                self.store.create_product, update.effective_user.id, *fields, True)
        except Exception:
            await self.failure(update)
            return PRODUCT_DETAILS
        context.user_data["last_input"]["product_id"] = product_id
        context.user_data["last_input"]["seller_number"] = seller_number
        return await self.product_saved(update, context, product_id, seller_number, compact=True)

    async def product_saved(self, update, context, product_id, seller_number, *, compact=False):
        # Record success before Telegram I/O; a lost reply must not allow another insert.
        context.user_data["pending_product_id"] = product_id
        context.user_data["pending_product_number"] = seller_number
        context.user_data.pop("product_draft", None)
        if not compact:
            context.user_data.pop("last_input", None)
        try:
            await update.effective_message.reply_text(
                f"Your product #{seller_number} saved (catalog ID #{product_id}). "
                "Send one product photo (max 5 MB), or /skip to finish without a photo. "
                "Cancelling now leaves the saved listing available. "
                + ("You can still correct the details by editing your message." if compact else
                   "Use the seller panel for later changes.")
            )
        except Exception:
            await self.failure(update)
        return PRODUCT_PHOTO

    async def product_entry_text(self, update, context):
        draft = context.user_data.get("product_draft")
        if not draft:
            await update.effective_message.reply_text("Product entry expired. Use /addproduct to start again.")
            return ConversationHandler.END
        message = update.effective_message
        field = draft["field"]
        if getattr(update, "edited_message", None) is not None:
            field = context.user_data["last_input"]["field"]
        validators = dict(name=clean_name, price_minor=clean_product_price,
                          stock_quantity=clean_product_stock, description=clean_product_description)
        if field not in validators:
            return None
        # A correction invalidates any later values and all previously issued buttons.
        for later in PRODUCT_FIELDS[PRODUCT_FIELDS.index(field):]:
            draft["fields"].pop(later, None)
        draft.update(field=field, token=None, message_id=None)
        self.remember(context, message, "product_entry", field=field)
        try:
            draft["fields"][field] = validators[field](message.text)
        except ValueError as exc:
            await message.reply_text(f"{exc} Try again or /cancel.")
            return PRODUCT_FIELD_STATES[field]
        next_field = PRODUCT_FIELDS[PRODUCT_FIELDS.index(field) + 1] if field != "description" else "review"
        return await self.product_entry_prompt(update, context, next_field)

    async def product_description_skip(self, update, context):
        draft = context.user_data["product_draft"]
        draft["fields"]["description"] = None
        context.user_data.pop("last_input", None)
        return await self.product_entry_prompt(update, context, "review")

    async def product_entry_prompt(self, update, context, field):
        draft = context.user_data["product_draft"]
        draft.update(field=field, token=None, message_id=None)
        options = {}
        if field in ("category", "review"):
            draft["token"] = uuid4().hex
            if field == "category":
                text = "Choose a product category."
                actions = [(label, key) for key, label in CATEGORIES.items()]
            else:
                values = draft["fields"]
                amount = Decimal(values["price_minor"]) / MINOR_UNITS_PER_MAJOR
                text = (f'Review your product (not saved yet):\n{values["name"]}\n'
                        f'Category: {category_label(values["category"])}\n'
                        f'Price: {DEFAULT_CURRENCY} {amount:.2f}\n'
                        f'Stock: {values["stock_quantity"]}\n'
                        f'Description: {values["description"] or "(none)"}')
                actions = [("Save product", "save"), ("Restart entry", "restart"), ("Cancel", "cancel")]
            options["reply_markup"] = InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=f"entry:{draft['token']}:{action}")]
                for label, action in actions])
        else:
            text = {"price_minor": "Send a price in USD, for example 19.99.",
                    "stock_quantity": "Send whole-number stock, for example 5 (0 is allowed).",
                    "description": "Send a description (max 1000 characters), or /skip to leave it empty."}[field]
        prompt = await update.effective_message.reply_text(text, **options)
        if field in ("category", "review"):
            draft["message_id"] = prompt.message_id
        return PRODUCT_REVIEW if field == "review" else PRODUCT_FIELD_STATES[field]

    async def product_entry_action(self, update, context):
        query = update.callback_query
        if update.effective_chat.type != "private":
            await query.answer("Please message me in a private chat.", show_alert=True)
            return None
        await query.answer()
        draft = context.user_data.get("product_draft")
        data = query.data
        match = re.fullmatch(r"entry:([0-9a-f]{32}):([a-z_]+)", data) if len(data.encode()) <= 64 else None
        if (not match or not draft or match[1] != draft.get("token")
                or query.message.message_id != draft.get("message_id")):
            await query.message.reply_text("This product entry button expired. Use /addproduct to start again.")
            return None
        action = match[2]
        if draft["field"] == "category" and action in CATEGORIES:
            draft["fields"]["category"] = action
            context.user_data.pop("last_input", None)
            return await self.product_entry_prompt(update, context, "price_minor")
        if draft["field"] != "review" or action not in ("save", "restart", "cancel"):
            await query.message.reply_text("Invalid product entry action. Use the current buttons or /cancel.")
            return None
        if action == "restart":
            return await self.add_product(update, context)
        if action == "cancel":
            return await self.cancel(update, context)
        # Revalidate the draft and live seller at the moment of confirmation.
        try:
            seller = await self.run(self.store.get_seller, update.effective_user.id)
            if not seller:
                self.reset_flow(context)
                await query.message.reply_text("Register a seller profile with /sellerprofile first.")
                return ConversationHandler.END
            values = draft["fields"]
            fields = (clean_name(values["name"]), clean_product_category(values["category"]),
                      clean_product_description(values["description"] or ""),
                      clean_product_price(str(Decimal(values["price_minor"]) / MINOR_UNITS_PER_MAJOR)),
                      clean_product_stock(str(values["stock_quantity"])))
            product_id, seller_number = await self.run(
                self.store.create_product, update.effective_user.id, *fields, True)
        except Exception:
            LOG.exception("Could not save guided product")
            await query.message.reply_text(
                "The save could not be confirmed. Check /myproducts before retrying, "
                "as the listing may already have been saved.")
            # Reissue review buttons; never retry an uncertain insert automatically.
            return await self.product_entry_prompt(update, context, "review")
        return await self.product_saved(update, context, product_id, seller_number)

    async def photo_for_product(self, update, context):
        product_id = context.user_data.get("pending_product_id")
        seller_number = context.user_data.get("pending_product_number")
        if product_id is None:
            await update.effective_message.reply_text(
                "Use /addproduct or /photo <your product number> first."
            )
            return ConversationHandler.END
        photo = update.effective_message.photo[-1]
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            await update.effective_message.reply_text("Photo is too large. Send one under 5 MB or /skip.")
            return PRODUCT_PHOTO
        try:
            telegram_file = await photo.get_file()
            output = io.BytesIO()
            await telegram_file.download_to_memory(out=output)
            raw = output.getvalue()
            if len(raw) > MAX_PHOTO_BYTES:
                raise ValueError("Photo is too large")
            key, version_id = await self.run(self.media.put_product_photo,
                                             update.effective_user.id, product_id, raw)
            try:
                await self.run(self.store.set_product_photo,
                               update.effective_user.id, product_id, key, version_id)
            except Exception:
                if version_id:
                    try:
                        await self.run(self.media.delete_version, key, version_id)
                    except Exception:
                        LOG.exception("Could not remove orphan photo version: %s", key)
                else:
                    LOG.error("Photo uploaded without version ID; inspect orphan key: %s", key)
                raise
        except ValueError as exc:
            await update.effective_message.reply_text(f"{exc}. Try another photo or /skip.")
            return PRODUCT_PHOTO
        except Exception:
            await self.failure(update)
            return PRODUCT_PHOTO
        await ProductPhotoDelivery(self.store, self.media).remember(
            dict(product_id=product_id, photo_object_key=key, photo_version_id=version_id),
            context.bot.id, photo.file_id, getattr(update, 'update_id', None))
        context.user_data.pop("pending_product_id", None)
        context.user_data.pop("pending_product_number", None)
        label = (f"your product #{seller_number}" if seller_number
                 else f"product #{product_id}")
        await update.effective_message.reply_text(f"Photo saved for {label}.")
        return ConversationHandler.END

    async def add_photo(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        if not context.args:
            await update.effective_message.reply_text(
                "Send your product number to add a photo (or G plus its catalog ID)."
            )
            return PHOTO_ID
        try:
            product = await self.owned_product(update.effective_user.id, context.args[0])
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return ConversationHandler.END
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not product:
            await update.effective_message.reply_text("That product is not yours.")
            return ConversationHandler.END
        if product["photo_object_key"]:
            await update.effective_message.reply_text("This product already has a photo.")
            return ConversationHandler.END
        context.user_data["pending_product_id"] = product["product_id"]
        context.user_data["pending_product_number"] = product["seller_number"]
        await update.effective_message.reply_text("Send one photo (max 5 MB), or /skip.")
        return PRODUCT_PHOTO

    async def skip(self, update, context):
        if context.user_data.pop("pending_product_id", None) is None:
            await update.effective_message.reply_text("There is no product photo to skip.")
            return ConversationHandler.END
        context.user_data.pop("pending_product_number", None)
        await update.effective_message.reply_text(
            "Done. You can add a photo later with /photo <your product number>."
        )
        return ConversationHandler.END

    async def products(self, update, context):
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
            key = context.args[0].lower() if context.args else None
            if key and key not in CATEGORIES and key != "all":
                await update.effective_message.reply_text("Use /products all, clothing, hardware, food, or art.")
                return
            category = (key if key != "all" else None) if key else (
                user["preferred_category"] if user else None
            )
        except Exception:
            await self.failure(update)
            return
        await self.product_page(update, "all", category)

    async def my_products(self, update, context):
        await self.product_page(update, "mine")

    async def product_page(self, update, scope, category=None, before_id=None):
        """Render live results; the button carries filters, never a seller identity."""
        seller_view = scope == "mine"
        try:
            products, has_more = await self.run(
                self.store.list_products_page, category,
                update.effective_user.id if seller_view else None, before_id=before_id,
            )
        except Exception:
            await self.failure(update)
            return

        navigation = []
        prefix = f"prodpage:{scope}:{category or 'all'}:"
        if has_more:
            navigation.append(InlineKeyboardButton(
                "Older products", callback_data=prefix + str(products[-1]["product_id"])))
        if before_id is not None:
            navigation.append(InlineKeyboardButton("Newest products", callback_data=prefix + "new"))
        options = {"reply_markup": InlineKeyboardMarkup([navigation])} if navigation else {}
        if not products:
            text = ("No older products remain. Return to the newest products."
                    if before_id is not None else
                    "You have no products yet. Use /addproduct." if seller_view else
                    "No products yet in that category. Try /products all.")
            await update.effective_message.reply_text(text, **options)
            return
        for offset in range(0, len(products), 5):
            await update.effective_message.reply_text(
                "\n\n".join(product_text(product, seller_view=seller_view)
                            for product in products[offset:offset + 5])
            )
        if seller_view:
            if navigation:
                await update.effective_message.reply_text("Your products: browse more below.", **options)
        else:
            await update.effective_message.reply_text(
                "Use /product <catalog ID> to view a photo. The ID is the #number "
                "before each product name.", **options,
            )

    async def product_page_action(self, update, context):
        """Durable navigation must bypass conversation hints without changing state."""
        query = update.callback_query
        try:
            if update.effective_chat.type != "private":
                await query.answer("Please message me in a private chat.", show_alert=True)
                return
            await query.answer()
            try:
                data = query.data
                if len(data.encode("utf-8")) > 64:
                    raise ValueError
                _, scope, category, raw = data.split(":")
                if (scope not in ("all", "mine")
                        or category not in (*CATEGORIES, "all")
                        or (scope == "mine" and category != "all")):
                    raise ValueError
                before_id = None
                if raw != "new":
                    if not re.fullmatch(r"[0-9]{1,19}", raw):
                        raise ValueError
                    before_id = int(raw)
                    if not 0 < before_id <= MAX_PRODUCT_ID:
                        raise ValueError
            except ValueError:
                await query.message.reply_text(
                    "Invalid product page. Use /products or /myproducts to browse again.")
                return
            await self.product_page(update, scope, None if category == "all" else category, before_id)
        except Exception:
            await self.failure(update)
        finally:
            # Stop subsequent handler groups, including active conversation catch-alls.
            raise ApplicationHandlerStop

    async def product(self, update, context):
        try:
            product_id = parse_catalog_id(context.args[0])
            lookup_started = monotonic()
            product = await self.run(self.store.get_product, product_id)
            lookup_ms = (monotonic() - lookup_started) * 1000
        except (IndexError, ValueError):
            await update.effective_message.reply_text(
                "Use /product <catalog ID>, for example /product 1 or /product #1."
            )
            return
        except Exception:
            await self.failure(update)
            return
        if not product:
            await update.effective_message.reply_text("Product not found.")
            return
        def details(current):
            caption = product_text(
                current, detail=True,
                seller_view=current["seller_telegram_user_id"] == update.effective_user.id,
            )
            buttons = self.order_handlers.product_buttons(current, update.effective_user.id)
            return caption, {"reply_markup": buttons} if buttons else {}

        if not product["photo_object_key"]:
            caption, options = details(product)
            await update.effective_message.reply_text(caption, **options)
            return
        try:
            await ProductPhotoDelivery(self.store, self.media).send(
                update, context.bot.id, product, details, lookup_ms=lookup_ms)
        except Exception as exc:
            # Avoid logging Telegram errors that might include a photo reference.
            LOG.warning('Product photo failed update_id=%s product_id=%s error_type=%s',
                        getattr(update, 'update_id', None), product_id, type(exc).__name__)
            await update.effective_message.reply_text(
                "The service is unavailable. Please try again later.")

    async def view_product(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        if not context.args:
            await update.effective_message.reply_text(CATALOG_ID_PROMPT)
            return PRODUCT_ID
        await self.product(update, context)
        return ConversationHandler.END

    async def change_stock(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        if not context.args:
            await update.effective_message.reply_text(
                "Send your product number and new stock, for example: 1 5. "
                "Use G3 5 for catalog ID #3."
            )
            return STOCK_INPUT
        await self.stock(update, context)
        return ConversationHandler.END

    async def stock(self, update, context):
        try:
            reference, quantity_text = context.args
            quantity = int(quantity_text)
            if not 0 <= quantity < 10**10:
                raise ValueError()
            product = await self.owned_product(update.effective_user.id, reference)
        except ValueError:
            await update.effective_message.reply_text(
                "Use /stock <your product number> <nonnegative quantity>. "
                "Prefix a catalog ID with G."
            )
            return
        except Exception:
            await self.failure(update)
            return
        if not product:
            await update.effective_message.reply_text(
                "That product is not yours or does not exist."
            )
            return
        try:
            changed = await self.run(self.store.set_stock, update.effective_user.id,
                                     product["product_id"], quantity)
        except Exception:
            await self.failure(update)
            return
        await update.effective_message.reply_text(
            f'Stock updated for your product #{product["seller_number"]}.' if changed
            else "That product is not yours or does not exist."
        )

    async def cancel(self, update, context):
        self.reset_flow(context)
        await update.effective_message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
