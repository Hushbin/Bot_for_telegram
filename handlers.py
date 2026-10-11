"""Telegram conversations backed by Oracle and private OCI Object Storage."""

import asyncio
import io
import logging
import re
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler


LOG = logging.getLogger(__name__)
CHOOSING, CLASS_STATE, SME_DETAILS, CHOOSE_PREF, PRODUCT_DETAILS, PRODUCT_PHOTO = range(6)
CATEGORIES = {
    "clothing": "Clothing/Fashion",
    "hardware": "Hardware Accessories",
    "food": "Food/Kitchen Ware",
    "art": "Art & Design",
}
MAX_PHOTO_BYTES = 5 * 1024 * 1024


def parse_contact(text):
    fields = [part.strip() for part in text.split(",")]
    if len(fields) != 3:
        raise ValueError("Enter name, email, phone separated by commas.")
    name, email, phone = fields
    if not (1 <= len(name) <= 160 and len(email) <= 254 and len(phone) <= 40):
        raise ValueError("Name, email, and phone are required and must fit their limits.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid email address.")
    if not re.fullmatch(r"\+?[0-9 ()-]{7,40}", phone):
        raise ValueError("Enter a valid phone number.")
    return name, email, phone


def parse_business(text):
    fields = [part.strip() for part in text.split(",")]
    if len(fields) != 4:
        raise ValueError("Enter business name, email, address, phone separated by commas.")
    name, email, address, phone = fields
    parse_contact(f"{name},{email},{phone}")
    if not address or len(address) > 400:
        raise ValueError("Enter a business address of at most 400 characters.")
    return name, email, address, phone


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
        f'Price: NGN {amount:.2f} · In stock: {product["stock_quantity"]}\n'
        f'{description}'
    ).strip()


class BotHandlers:
    def __init__(self, store, media):
        self.store = store
        self.media = media

    async def run(self, operation, *args):
        return await asyncio.to_thread(operation, *args)

    async def private_only(self, update):
        if update.effective_chat.type == "private":
            return False
        await update.effective_message.reply_text("Please message me in a private chat.")
        return True

    async def failure(self, update):
        LOG.exception("Bot operation failed")
        await update.effective_message.reply_text("The service is unavailable. Please try again later.")

    async def start(self, update, context):
        if await self.private_only(update):
            return ConversationHandler.END
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if user:
            await update.message.reply_text(
                f'Welcome back, {user["full_name"]}!\n'
                "Use /products to browse, /profile to update your details, "
                "/preferences for categories, or /sellerprofile to sell."
            )
            return ConversationHandler.END
        await update.message.reply_text(
            "Welcome to SMEbot! Please send your full name, email, and phone number "
            "separated by commas. Example: Jane Doe, jane@example.com, +1234567890\n"
            "Use /cancel to stop."
        )
        return CHOOSING

    async def profile(self, update, context):
        if await self.private_only(update):
            return ConversationHandler.END
        await update.message.reply_text("Send your full name, email, and phone separated by commas.")
        return CHOOSING

    async def seller_profile(self, update, context):
        if await self.private_only(update):
            return ConversationHandler.END
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not user:
            await update.message.reply_text("Register your personal details with /start first.")
            return ConversationHandler.END
        await update.message.reply_text(
            "Send business name, email, address, phone separated by commas."
        )
        return SME_DETAILS

    async def preferences(self, update, context):
        if await self.private_only(update):
            return ConversationHandler.END
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not user:
            await update.message.reply_text("Register with /start first.")
            return ConversationHandler.END
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"cat:{key}")]
            for key, name in CATEGORIES.items()
        ]
        await update.message.reply_text(
            "Choose a category you like:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return CHOOSE_PREF

    async def choose(self, update, context):
        try:
            name, email, phone = parse_contact(update.message.text)
        except ValueError as exc:
            await update.message.reply_text(f"{exc} Try again or /cancel.")
            return CHOOSING
        try:
            user = await self.run(self.store.upsert_user, update.effective_user.id,
                                  name, email, phone)
        except Exception:
            await self.failure(update)
            return CHOOSING
        if user["role"] == "SELLER":
            await update.message.reply_text("Profile updated. Use /addproduct or /myproducts.")
            return ConversationHandler.END
        keyboard = [[
            InlineKeyboardButton("Seller", callback_data="role:seller"),
            InlineKeyboardButton("Customer", callback_data="role:customer"),
        ]]
        await update.message.reply_text(
            f"Thanks, {name}. Are you a seller or a customer?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CLASS_STATE

    async def classer(self, update, context):
        query = update.callback_query
        await query.answer()
        if query.data == "role:seller":
            await query.message.reply_text(
                "Tell me about your business: name, email, address, phone, "
                "separated by commas."
            )
            return SME_DETAILS
        if query.data == "role:customer":
            buttons = [
                [InlineKeyboardButton(name, callback_data=f"cat:{key}")]
                for key, name in CATEGORIES.items()
            ]
            await query.message.reply_text(
                "Choose a category you like:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return CHOOSE_PREF
        return CLASS_STATE

    async def seller_details(self, update, context):
        try:
            business = parse_business(update.message.text)
        except ValueError as exc:
            await update.message.reply_text(f"{exc} Try again or /cancel.")
            return SME_DETAILS
        try:
            await self.run(self.store.upsert_seller, update.effective_user.id, *business)
        except Exception:
            await self.failure(update)
            return SME_DETAILS
        await update.message.reply_text(
            "Seller profile saved. Use /addproduct to list your first product."
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
        await query.message.reply_text(
            f"Saved {CATEGORIES[key]}. Use /products to see available products."
        )
        return ConversationHandler.END

    async def add_product(self, update, context):
        if await self.private_only(update):
            return ConversationHandler.END
        try:
            seller = await self.run(self.store.get_seller, update.effective_user.id)
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not seller:
            await update.message.reply_text("Register a seller profile with /sellerprofile first.")
            return ConversationHandler.END
        await update.message.reply_text(
            "Send: name | category | price in NGN | stock | description\n"
            "Category keys: clothing, hardware, food, art.\n"
            "Example: Blue shirt | clothing | 19.99 | 5 | Cotton shirt"
        )
        return PRODUCT_DETAILS

    async def product_details(self, update, context):
        try:
            fields = parse_product(update.message.text)
        except ValueError as exc:
            await update.message.reply_text(f"{exc} Try again or /cancel.")
            return PRODUCT_DETAILS
        try:
            product_id = await self.run(self.store.create_product,
                                        update.effective_user.id, *fields)
        except Exception:
            await self.failure(update)
            return PRODUCT_DETAILS
        context.user_data["pending_product_id"] = product_id
        await update.message.reply_text(
            f"Product #{product_id} saved. Send one product photo (max 5 MB), "
            "or /skip to finish without a photo."
        )
        return PRODUCT_PHOTO

    async def photo_for_product(self, update, context):
        product_id = context.user_data.get("pending_product_id")
        if product_id is None:
            await update.message.reply_text("Use /addproduct or /photo <product ID> first.")
            return ConversationHandler.END
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            await update.message.reply_text("Photo is too large. Send one under 5 MB or /skip.")
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
                               update.effective_user.id, product_id, key)
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
            await update.message.reply_text(f"{exc}. Try another photo or /skip.")
            return PRODUCT_PHOTO
        except Exception:
            await self.failure(update)
            return PRODUCT_PHOTO
        context.user_data.pop("pending_product_id", None)
        await update.message.reply_text(f"Photo saved for product #{product_id}.")
        return ConversationHandler.END

    async def add_photo(self, update, context):
        if await self.private_only(update):
            return ConversationHandler.END
        try:
            product_id = int(context.args[0])
            product = await self.run(self.store.get_product, product_id)
        except (IndexError, ValueError):
            await update.message.reply_text("Use /photo <product ID>.")
            return ConversationHandler.END
        except Exception:
            await self.failure(update)
            return ConversationHandler.END
        if not product or product["seller_telegram_user_id"] != update.effective_user.id:
            await update.message.reply_text("That product is not yours.")
            return ConversationHandler.END
        if product["photo_object_key"]:
            await update.message.reply_text("This product already has a photo.")
            return ConversationHandler.END
        context.user_data["pending_product_id"] = product_id
        await update.message.reply_text("Send one photo (max 5 MB), or /skip.")
        return PRODUCT_PHOTO

    async def skip(self, update, context):
        context.user_data.pop("pending_product_id", None)
        await update.message.reply_text("Done. You can add a photo later with /photo <product ID>.")
        return ConversationHandler.END

    async def products(self, update, context):
        if await self.private_only(update):
            return
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
            key = context.args[0].lower() if context.args else None
            if key and key not in CATEGORIES and key != "all":
                await update.message.reply_text("Use /products all, clothing, hardware, food, or art.")
                return
            category = (CATEGORIES[key] if key != "all" else None) if key else (
                user["preferred_category"] if user else None
            )
            products = await self.run(self.store.list_products, category, None)
        except Exception:
            await self.failure(update)
            return
        if not products:
            await update.message.reply_text("No products yet in that category. Try /products all.")
            return
        for offset in range(0, len(products), 5):
            await update.message.reply_text(
                "\n\n".join(product_text(product) for product in products[offset:offset + 5])
            )
        await update.message.reply_text("Use /product <ID> to view a photo.")

    async def my_products(self, update, context):
        if await self.private_only(update):
            return
        try:
            products = await self.run(self.store.list_products, None,
                                      update.effective_user.id)
        except Exception:
            await self.failure(update)
            return
        if not products:
            await update.message.reply_text("You have no products yet. Use /addproduct.")
            return
        for offset in range(0, len(products), 5):
            await update.message.reply_text(
                "\n\n".join(product_text(product) for product in products[offset:offset + 5])
            )

    async def product(self, update, context):
        if await self.private_only(update):
            return
        try:
            product_id = int(context.args[0])
            product = await self.run(self.store.get_product, product_id)
        except (IndexError, ValueError):
            await update.message.reply_text("Use /product <ID>.")
            return
        except Exception:
            await self.failure(update)
            return
        if not product:
            await update.message.reply_text("Product not found.")
            return
        caption = product_text(product, detail=True)
        if not product["photo_object_key"]:
            await update.message.reply_text(caption)
            return
        try:
            raw = await self.run(self.media.get_photo, product["photo_object_key"])
            await update.message.reply_photo(photo=io.BytesIO(raw), caption=caption)
        except Exception:
            await self.failure(update)

    async def stock(self, update, context):
        if await self.private_only(update):
            return
        try:
            product_id, quantity = map(int, context.args)
            if product_id <= 0 or not 0 <= quantity < 10**10:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("Use /stock <product ID> <nonnegative quantity>.")
            return
        try:
            changed = await self.run(self.store.set_stock, update.effective_user.id,
                                     product_id, quantity)
        except Exception:
            await self.failure(update)
            return
        await update.message.reply_text(
            f"Stock updated for product #{product_id}." if changed
            else "That product is not yours or does not exist."
        )

    async def cancel(self, update, context):
        context.user_data.pop("pending_product_id", None)
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END
