"""Telegram conversations backed by Oracle and private OCI Object Storage."""

import asyncio
import io
import logging
import re
from decimal import Decimal, InvalidOperation

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import ConversationHandler


LOG = logging.getLogger(__name__)
(CHOOSING, CLASS_STATE, SME_DETAILS, CHOOSE_PREF, PRODUCT_DETAILS, PRODUCT_PHOTO,
 PHONE, CONFIRM_ERASE) = range(8)
CATEGORIES = {
    "clothing": "Clothing/Fashion",
    "hardware": "Hardware Accessories",
    "food": "Food/Kitchen Ware",
    "art": "Art & Design",
}
MAX_PHOTO_BYTES = 5 * 1024 * 1024

PRIVACY_NOTICE = (
    "What I store about you:\n"
    "- your Telegram ID, name, email and phone number (and whether Telegram "
    "verified the number), your role and your preferred category;\n"
    "- sellers also give a business name, email, address and phone number, and "
    "list products with photos.\n\n"
    "Who can see it: the bot's operator. Other users only see what sellers list: "
    "the business name, product names, descriptions, prices, stock and photos. "
    "Emails, addresses and phone numbers are not shown to other users.\n\n"
    "How long: until you erase it. There is no automatic expiry.\n\n"
    "Your controls: /mydata shows what is stored. /deleteme erases your personal "
    "data, seller profile, products and photos; only your numeric Telegram ID and "
    "the time of erasure are kept."
)
SELLER_NOTICE = (
    "These business details are stored with your profile (see /privacy). Your business "
    "name and your products are visible to other users; the email, address and phone "
    "are not."
)
BUSINESS_PROMPT = "Send business name, email, address, phone separated by commas."

EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
PHONE_PATTERN = re.compile(r"\+?[0-9 ()-]{7,40}")


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


def parse_business(text):
    fields = [part.strip() for part in text.split(",")]
    if len(fields) < 4:
        raise ValueError("Enter business name, email, address, phone separated by commas.")
    # The address may itself contain commas, so it is everything between email and phone.
    name, email, phone = fields[0], fields[1], fields[-1]
    address = ", ".join(fields[2:-1])
    if not address or len(address) > 400:
        raise ValueError("Enter a business address of at most 400 characters.")
    return clean_name(name), clean_email(email), address, clean_phone(phone)


def parse_product(text):
    fields = [part.strip() for part in text.split("|", 4)]
    if len(fields) != 5:
        raise ValueError("Use: name | category | price | stock | description")
    name, category_key, price_text, stock_text, description = fields
    category = CATEGORIES.get(category_key.lower())
    if not 1 <= len(name) <= 160 or category is None or len(description) > 1000:
        raise ValueError("Check name, category key, and description length (max 1000).")
    try:
        price = Decimal(price_text)
        cents = price * 100
        if not price.is_finite() or price < 0 or cents != cents.to_integral_value():
            raise ValueError("Price must be a nonnegative amount with at most two decimals.")
        price_kobo = int(cents)
        quantity = int(stock_text)
        if not 0 <= price_kobo < 10**12 or not 0 <= quantity < 10**10:
            raise ValueError("Price or stock is out of range.")
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError("Enter a valid nonnegative price and whole-number stock.") from exc
    return name, category, description or None, price_kobo, quantity


def product_text(product, *, detail=False):
    amount = Decimal(product["price_kobo"]) / 100
    description = product["description"] or ""
    description = description[:400 if detail else 140]
    return (
        f'#{product["product_id"]} {product["name"]}\n'
        f'{product["business_name"]} · {product["category"]}\n'
        f'Price: USD {amount:.2f} · In stock: {product["stock_quantity"]}\n'
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
        f'Role: {user["role"].lower()}',
        f'Preferred category: {user["preferred_category"] or "none"}',
    ]
    if seller:
        lines += [
            "",
            "Seller profile:",
            f'Business name: {seller["business_name"]}',
            f'Business email: {seller["business_email"]}',
            f'Address: {seller["address"]}',
            f'Business phone: {seller["business_phone"]}',
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
        resize_keyboard=True, one_time_keyboard=True,
    )


class BotHandlers:
    def __init__(self, store, media):
        self.store = store
        self.media = media

    async def run(self, operation, *args):
        return await asyncio.to_thread(operation, *args)

    @staticmethod
    def reset_flow(context):
        """Forget the half-finished input of whatever flow the user was in."""
        for key in ("last_input", "draft", "pending_product_id"):
            context.user_data.pop(key, None)

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
            "contact": self.choose,
            "phone": self.phone_text,
            "business": self.seller_details,
            "product": self.product_details,
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

    async def delete_me(self, update, context):
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
            "This permanently erases your name, email and phone number, your seller "
            "profile, your products and their photos. Only your numeric Telegram ID and "
            "the time of erasure are kept. This cannot be undone. Continue?",
            reply_markup=buttons,
        )
        return CONFIRM_ERASE

    async def confirm_erase(self, update, context):
        query = update.callback_query
        await query.answer()
        if query.data != "erase:yes":
            await query.message.reply_text("Nothing was erased.")
            return ConversationHandler.END
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
        self.reset_flow(context)
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if user:
            await update.effective_message.reply_text(
                f'Welcome back, {user["full_name"]}!\n'
                "Use /products to browse, /profile to update your details, "
                "/preferences for categories, /sellerprofile to sell, "
                "/mydata to see what is stored, or /deleteme to erase it."
            )
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Welcome to SMEbot! Before we start, here is what I store.\n\n" + PRIVACY_NOTICE
        )
        await update.effective_message.reply_text(
            "Please send your full name and email separated by a comma. "
            "Example: Jane Doe, jane@example.com\nUse /cancel to stop."
        )
        return CHOOSING

    async def profile(self, update, context):
        self.reset_flow(context)
        await update.effective_message.reply_text(
            "Send your full name and email separated by a comma. "
            "Example: Jane Doe, jane@example.com (see /privacy for what is stored)."
        )
        return CHOOSING

    async def seller_profile(self, update, context):
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
        await message.reply_text(
            f"Thanks, {name}. Now your phone number. Tap the button to share the number "
            "verified by Telegram, or type a different number, for example +1234567890.",
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
            await message.reply_text("Let's start again: send /start.",
                                     reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        try:
            user = await self.run(self.store.upsert_user, update.effective_user.id,
                                  draft["name"], draft["email"], phone, verified)
        except Exception:
            await self.failure(update)
            return PHONE
        await message.reply_text(
            "Phone number saved" + (" (verified by Telegram)." if verified else "."),
            reply_markup=ReplyKeyboardRemove(),
        )
        if user["role"] == "SELLER":
            await message.reply_text("Profile updated. Use /addproduct or /myproducts.")
            return ConversationHandler.END
        keyboard = [[
            InlineKeyboardButton("Seller", callback_data="role:seller"),
            InlineKeyboardButton("Customer", callback_data="role:customer"),
        ]]
        await message.reply_text(
            f'Thanks, {draft["name"]}. Are you a seller or a customer?',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CLASS_STATE

    async def classer(self, update, context):
        query = update.callback_query
        await query.answer()
        if query.data == "role:seller":
            self.reset_flow(context)
            await query.message.reply_text(
                f"Tell me about your business. {BUSINESS_PROMPT}\n\n{SELLER_NOTICE}"
            )
            return SME_DETAILS
        if query.data == "role:customer":
            self.reset_flow(context)
            await query.message.reply_text(
                "Choose a category you like:", reply_markup=category_buttons()
            )
            return CHOOSE_PREF
        return CLASS_STATE

    async def seller_details(self, update, context):
        message = update.effective_message
        corrected = getattr(update, "edited_message", None) is not None
        self.remember(context, message, "business")
        try:
            business = parse_business(message.text)
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
            else "Seller profile saved. Use /addproduct to list your first product."
        )
        return ConversationHandler.END

    async def preference(self, update, context):
        query = update.callback_query
        await query.answer()
        key = query.data.removeprefix("cat:")
        if key not in CATEGORIES:
            return CHOOSE_PREF
        try:
            await self.run(self.store.set_preference, update.effective_user.id,
                           CATEGORIES[key])
        except Exception:
            await self.failure(update)
            return CHOOSE_PREF
        self.reset_flow(context)
        await query.message.reply_text(
            f"Saved {CATEGORIES[key]}. Use /products to see available products."
        )
        return ConversationHandler.END

    async def add_product(self, update, context):
        self.reset_flow(context)
        try:
            seller = await self.run(self.store.get_seller, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not seller:
            await update.effective_message.reply_text("Register a seller profile with /sellerprofile first.")
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Send: name | category | price in USD | stock | description\n"
            "Category keys: clothing, hardware, food, art.\n"
            "Example: Blue shirt | clothing | 19.99 | 5 | Cotton shirt"
        )
        return PRODUCT_DETAILS

    async def product_details(self, update, context):
        message = update.effective_message
        last = context.user_data.get("last_input") or {}
        # An edit of the line that already created a product updates that product.
        product_id = None
        if (getattr(update, "edited_message", None) is not None
                and last.get("step") == "product" and last.get("id") == message.message_id):
            product_id = last.get("product_id")
        retry_state = PRODUCT_PHOTO if product_id else PRODUCT_DETAILS
        self.remember(context, message, "product", **({"product_id": product_id} if product_id else {}))
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
                f"Product #{product_id} updated." if changed
                else "That product no longer exists."
            )
            return PRODUCT_PHOTO if changed else ConversationHandler.END
        try:
            product_id = await self.run(self.store.create_product,
                                        update.effective_user.id, *fields)
        except Exception:
            await self.failure(update)
            return PRODUCT_DETAILS
        context.user_data["pending_product_id"] = product_id
        context.user_data["last_input"]["product_id"] = product_id
        await message.reply_text(
            f"Product #{product_id} saved. Send one product photo (max 5 MB), "
            "or /skip to finish without a photo. You can still correct the details by "
            "editing your message."
        )
        return PRODUCT_PHOTO

    async def photo_for_product(self, update, context):
        product_id = context.user_data.get("pending_product_id")
        if product_id is None:
            await update.effective_message.reply_text("Use /addproduct or /photo <product ID> first.")
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
        context.user_data.pop("pending_product_id", None)
        await update.effective_message.reply_text(f"Photo saved for product #{product_id}.")
        return ConversationHandler.END

    async def add_photo(self, update, context):
        self.reset_flow(context)
        try:
            product_id = int(context.args[0])
            product = await self.run(self.store.get_product, product_id)
        except (IndexError, ValueError):
            await update.effective_message.reply_text("Use /photo <product ID>.")
            return ConversationHandler.END
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not product or product["seller_telegram_user_id"] != update.effective_user.id:
            await update.effective_message.reply_text("That product is not yours.")
            return ConversationHandler.END
        if product["photo_object_key"]:
            await update.effective_message.reply_text("This product already has a photo.")
            return ConversationHandler.END
        context.user_data["pending_product_id"] = product_id
        await update.effective_message.reply_text("Send one photo (max 5 MB), or /skip.")
        return PRODUCT_PHOTO

    async def skip(self, update, context):
        context.user_data.pop("pending_product_id", None)
        await update.effective_message.reply_text("Done. You can add a photo later with /photo <product ID>.")
        return ConversationHandler.END

    async def products(self, update, context):
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
            key = context.args[0].lower() if context.args else None
            if key and key not in CATEGORIES and key != "all":
                await update.effective_message.reply_text("Use /products all, clothing, hardware, food, or art.")
                return
            category = (CATEGORIES[key] if key != "all" else None) if key else (
                user["preferred_category"] if user else None
            )
            products = await self.run(self.store.list_products, category, None)
        except Exception:
            await self.failure(update)
            return
        if not products:
            await update.effective_message.reply_text("No products yet in that category. Try /products all.")
            return
        for offset in range(0, len(products), 5):
            await update.effective_message.reply_text(
                "\n\n".join(product_text(product) for product in products[offset:offset + 5])
            )
        await update.effective_message.reply_text("Use /product <ID> to view a photo.")

    async def my_products(self, update, context):
        try:
            products = await self.run(self.store.list_products, None,
                                      update.effective_user.id)
        except Exception:
            await self.failure(update)
            return
        if not products:
            await update.effective_message.reply_text("You have no products yet. Use /addproduct.")
            return
        for offset in range(0, len(products), 5):
            await update.effective_message.reply_text(
                "\n\n".join(product_text(product) for product in products[offset:offset + 5])
            )

    async def product(self, update, context):
        try:
            product_id = int(context.args[0])
            product = await self.run(self.store.get_product, product_id)
        except (IndexError, ValueError):
            await update.effective_message.reply_text("Use /product <ID>.")
            return
        except Exception:
            await self.failure(update)
            return
        if not product:
            await update.effective_message.reply_text("Product not found.")
            return
        caption = product_text(product, detail=True)
        if not product["photo_object_key"]:
            await update.effective_message.reply_text(caption)
            return
        try:
            raw = await self.run(self.media.get_photo, product["photo_object_key"])
            await update.effective_message.reply_photo(photo=io.BytesIO(raw), caption=caption)
        except Exception:
            await self.failure(update)

    async def stock(self, update, context):
        try:
            product_id, quantity = map(int, context.args)
            if product_id <= 0 or not 0 <= quantity < 10**10:
                raise ValueError()
        except ValueError:
            await update.effective_message.reply_text("Use /stock <product ID> <nonnegative quantity>.")
            return
        try:
            changed = await self.run(self.store.set_stock, update.effective_user.id,
                                     product_id, quantity)
        except Exception:
            await self.failure(update)
            return
        await update.effective_message.reply_text(
            f"Stock updated for product #{product_id}." if changed
            else "That product is not yours or does not exist."
        )

    async def cancel(self, update, context):
        self.reset_flow(context)
        await update.effective_message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
