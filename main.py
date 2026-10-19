"""SMEbot entry point. Uses long polling, both locally and on the OCI VM."""

import logging

from telegram import BotCommand, MenuButtonCommands, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import load_settings
from order_handlers import ORDER_ID, ORDER_QUANTITY, ORDER_CONFIRM
from order_worker import OrderWorker
from process_readiness import ProcessReadiness, ReadinessBot
from handlers import (
    BotHandlers, CHOOSING, CLASS_STATE, SME_DETAILS, CHOOSE_PREF,
    PRODUCT_DETAILS, PRODUCT_PHOTO, PHONE, CONFIRM_ERASE, PRODUCT_ID, PHOTO_ID,
    STOCK_INPUT, BROWSE_CATEGORY, on_error,
    PRODUCT_CATEGORY, PRODUCT_PRICE, PRODUCT_STOCK, PRODUCT_DESCRIPTION, PRODUCT_REVIEW,
)


ALLOWED_UPDATES = [Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY]

# New private messages are handled as input. Edited messages are routed separately to
# `handlers.edited`, which applies an edit only to the user's latest input. Group chats
# must not be able to start a registration or a product listing.
NEW_MESSAGE = filters.UpdateType.MESSAGE
PRIVATE_COMMAND = NEW_MESSAGE & filters.ChatType.PRIVATE
PRIVATE_TEXT = PRIVATE_COMMAND & filters.TEXT & ~filters.COMMAND
PRIVATE_NON_COMMAND = PRIVATE_COMMAND & ~filters.COMMAND
PRIVATE_EDITED_TEXT = (filters.UpdateType.EDITED_MESSAGE & filters.ChatType.PRIVATE
                       & filters.TEXT & ~filters.COMMAND)

BOT_COMMANDS = (
    "menu", "start", "profile", "sellerprofile", "preferences", "addproduct",
    "photo", "products", "product", "myproducts", "stock", "privacy",
    "mydata", "deleteme", "cancel", "skip", "order", "orders", "sales", "panel",
)

COMMAND_DESCRIPTIONS = (
    ("menu", "Open the button menu"),
    ("start", "Register or see your welcome screen"),
    ("products", "Browse products"),
    ("product", "View a product by ID"),
    ("profile", "Update your name, email and phone"),
    ("preferences", "Choose a preferred category"),
    ("sellerprofile", "Create or edit your seller profile"),
    ("addproduct", "List a product"),
    ("photo", "Add a photo to a product"),
    ("myproducts", "See your listings"),
    ("stock", "Change product stock"),
    ("order", "Order a product"),
    ("orders", "See my orders"),
    ("sales", "See incoming orders"),
    ("panel", "Open the seller web panel"),
    ("privacy", "Read the privacy notice"),
    ("mydata", "See your stored data"),
    ("deleteme", "Erase your data"),
    ("cancel", "Stop the current step"),
    ("skip", "Skip a product photo"),
)


async def register_command_menu(application):
    await application.bot.set_my_commands(
        [BotCommand(command, description) for command, description in COMMAND_DESCRIPTIONS]
    )
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def configure_logging():
    logging.basicConfig(level=logging.INFO)
    # httpx logs every request URL at INFO, and Telegram's URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def with_hint(handlers, prompt):
    """Append catch-alls so unmatched input gets a reply instead of silence.

    Commands are excluded on purpose: `/products`, `/cancel` etc. must keep working.
    The edit handler comes first so a corrected message is not mistaken for new input.
    """
    hint = handlers.waiting_for(prompt)
    return [
        MessageHandler(PRIVATE_EDITED_TEXT, handlers.edited),
        MessageHandler(PRIVATE_NON_COMMAND, hint),
        CallbackQueryHandler(hint),
    ]


def build_application(settings=None, store=None, media=None):
    readiness = ProcessReadiness.for_process()
    settings = settings or load_settings()
    if store is None or media is None:
        if settings.backend == "local":
            from local_backend import LocalMediaStorage, SQLiteStore
            store = store or SQLiteStore(settings.local_db_path)
            media = media or LocalMediaStorage(settings.local_media_dir)
        else:
            from database import OracleStore
            from storage import MediaStorage
            from recovery import RecoveryStorage
            store = store or OracleStore(settings)
            media = media or MediaStorage(settings)
    erasures = None
    if settings.backend == "oci":
        from recovery import RecoveryStorage
        erasures = RecoveryStorage(settings.oci_namespace, settings.oci_recovery_bucket,
                                   auth=settings.oci_auth,
                                   config_file=settings.oci_config_file,
                                   profile=settings.oci_profile)
    store.check()
    handlers = BotHandlers(store, media, erasures, getattr(settings, "orders_enabled", False))
    orders = handlers.order_handlers
    worker_holder = {}
    panel = None
    if getattr(settings, 'seller_panel_url', ''):
        from seller_api import SellerRuntime
        panel = SellerRuntime(store, settings.seller_panel_url, settings.seller_api_port,
                              settings.orders_enabled, media=media)

    async def start_command(update, context):
        if panel and context.args == ['seller_panel']:
            return await panel_command(update, context)
        return await handlers.start(update, context)

    async def panel_command(update, context):
        if panel:
            await handlers.hide_phone_keyboard(update, context)
            handlers.reset_flow(context)
            return await panel.panel(update, context)
        await update.effective_message.reply_text('The seller panel is not configured yet.')
        return ConversationHandler.END

    async def start_runtime(application):
        await register_command_menu(application)
        if panel:
            await panel.start(application)
        worker = OrderWorker(store, application.bot)
        worker_holder["worker"] = worker
        worker.start()
        guards = [lambda: worker.task is not None and not worker.task.done(),
                  lambda: application.running and application.updater.running]
        if panel:
            guards.append(lambda: panel.task is not None and not panel.task.done())
        readiness.runtime_started(*guards)

    async def stop_runtime(application):
        readiness.stop()
        if panel:
            await panel.stop()
        worker = worker_holder.get("worker")
        if worker is not None:
            await worker.stop()

    application = (ApplicationBuilder().bot(ReadinessBot(settings.bot_token, readiness))
                   .concurrent_updates(False).post_init(start_runtime)
                   .post_stop(stop_runtime).post_shutdown(stop_runtime).build())
    application.add_handler(CallbackQueryHandler(
        orders.durable_action, pattern=r"^(ord|hist):"), group=-1)
    application.add_handler(CallbackQueryHandler(
        handlers.product_page_action, pattern=r"^prodpage:"), group=-1)

    # One conversation for every multi-step flow. A user can only be in one state at a
    # time, any entry command replaces the current state, and one /cancel clears it.
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("order", orders.start, filters=PRIVATE_COMMAND),
            CallbackQueryHandler(orders.start, pattern=r"^buy:"),
            CallbackQueryHandler(orders.confirm, pattern=r"^(confirm|discard):[0-9a-f]{32}$"),
            CallbackQueryHandler(handlers.product_entry_action, pattern=r"^entry:"),
            CommandHandler("menu", handlers.menu, filters=PRIVATE_COMMAND),
            CallbackQueryHandler(handlers.menu_action, pattern=r"^menu:"),
            CommandHandler("start", start_command, filters=PRIVATE_COMMAND),
            CommandHandler("panel", panel_command, filters=PRIVATE_COMMAND),
            CommandHandler("profile", handlers.profile, filters=PRIVATE_COMMAND),
            CommandHandler("sellerprofile", handlers.seller_profile, filters=PRIVATE_COMMAND),
            CommandHandler("preferences", handlers.preferences, filters=PRIVATE_COMMAND),
            CommandHandler("addproduct", handlers.add_product, filters=PRIVATE_COMMAND),
            CommandHandler("photo", handlers.add_photo, filters=PRIVATE_COMMAND),
            CommandHandler("product", handlers.view_product, filters=PRIVATE_COMMAND),
            CommandHandler("stock", handlers.change_stock, filters=PRIVATE_COMMAND),
            CommandHandler("deleteme", handlers.delete_me, filters=PRIVATE_COMMAND),
        ],
        states={
            ORDER_ID: [MessageHandler(PRIVATE_TEXT, orders.product_id),
                       *with_hint(handlers, "Please send a catalog ID.")],
            ORDER_QUANTITY: [MessageHandler(PRIVATE_TEXT | PRIVATE_EDITED_TEXT, orders.quantity),
                             *with_hint(handlers, "Please send a quantity.")],
            ORDER_CONFIRM: [MessageHandler(PRIVATE_EDITED_TEXT, orders.quantity),
                            *with_hint(handlers, "Please confirm the order above or /cancel.")],
            CHOOSING: [
                MessageHandler(PRIVATE_TEXT, handlers.choose),
                *with_hint(handlers, "Please send your full name and email separated by a comma."),
            ],
            PHONE: [
                MessageHandler(PRIVATE_COMMAND & filters.CONTACT, handlers.phone_contact),
                MessageHandler(PRIVATE_TEXT, handlers.phone_text),
                *with_hint(handlers, "Please tap the button to share your Telegram number, "
                                     "or type a phone number."),
            ],
            CONFIRM_ERASE: [
                CallbackQueryHandler(handlers.confirm_erase, pattern=r"^erase:"),
                *with_hint(handlers, "Please tap a button above to confirm or keep your data."),
            ],
            CLASS_STATE: [
                CallbackQueryHandler(handlers.classer, pattern=r"^role:"),
                *with_hint(handlers, "Please tap Seller or Buyer in the message above."),
            ],
            SME_DETAILS: [
                MessageHandler(PRIVATE_TEXT, handlers.seller_details),
                *with_hint(handlers, "Send listing name | city/area (or Online). "
                                     "You can add | email | phone for different seller contact details."),
            ],
            CHOOSE_PREF: [
                CallbackQueryHandler(handlers.preference, pattern=r"^cat:"),
                *with_hint(handlers, "Please tap a category in the message above."),
            ],
            PRODUCT_DETAILS: [
                MessageHandler(PRIVATE_TEXT, handlers.product_details),
                *with_hint(handlers, "Please send a product name, or name | category | price | stock | description."),
            ],
            PRODUCT_CATEGORY: [*with_hint(handlers, "Please choose a product category above.")],
            PRODUCT_PRICE: [MessageHandler(PRIVATE_TEXT, handlers.product_entry_text),
                            *with_hint(handlers, "Please send a price in USD, for example 19.99.")],
            PRODUCT_STOCK: [MessageHandler(PRIVATE_TEXT, handlers.product_entry_text),
                            *with_hint(handlers, "Please send whole-number stock, for example 5.")],
            PRODUCT_DESCRIPTION: [CommandHandler("skip", handlers.product_description_skip,
                                                 filters=PRIVATE_COMMAND),
                                  MessageHandler(PRIVATE_TEXT, handlers.product_entry_text),
                                  *with_hint(handlers, "Please send a description or /skip.")],
            PRODUCT_REVIEW: [*with_hint(handlers, "Please review the product and use the buttons above.")],
            PRODUCT_PHOTO: [
                MessageHandler(PRIVATE_COMMAND & filters.PHOTO, handlers.photo_for_product),
                CommandHandler("skip", handlers.skip, filters=PRIVATE_COMMAND),
                *with_hint(handlers, "Please send a photo (not a file), or /skip."),
            ],
            PRODUCT_ID: [
                MessageHandler(PRIVATE_TEXT, handlers.product_id),
                *with_hint(handlers, "Please send a catalog ID from /products or /myproducts, "
                                     "for example 1 or #1."),
            ],
            PHOTO_ID: [
                MessageHandler(PRIVATE_TEXT, handlers.photo_id),
                *with_hint(handlers, "Please send your product number."),
            ],
            STOCK_INPUT: [
                MessageHandler(PRIVATE_TEXT, handlers.stock_input),
                *with_hint(handlers, "Please send your product number and quantity."),
            ],
            BROWSE_CATEGORY: [
                CallbackQueryHandler(handlers.browse_selection, pattern=r"^browse:"),
                *with_hint(handlers, "Please choose a product category above."),
            ],
        },
        fallbacks=[CommandHandler("cancel", handlers.cancel, filters=PRIVATE_COMMAND)],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    for name, callback in (
        ("orders", orders.purchases),
        ("sales", orders.sales),
        ("products", handlers.products),
        ("myproducts", handlers.my_products),
        ("privacy", handlers.privacy),
        ("mydata", handlers.my_data),
    ):
        application.add_handler(CommandHandler(name, callback, filters=PRIVATE_COMMAND))
    # Corrections to a message whose flow has already finished, e.g. the business details.
    application.add_handler(MessageHandler(PRIVATE_EDITED_TEXT, handlers.edited))
    application.add_handler(CommandHandler("cancel", handlers.cancel, filters=PRIVATE_COMMAND))
    application.add_handler(CommandHandler("skip", handlers.skip, filters=PRIVATE_COMMAND))
    # Commands sent in a group are answered once, with a pointer to the private chat.
    application.add_handler(CommandHandler(
        BOT_COMMANDS, handlers.not_private,
        filters=NEW_MESSAGE & ~filters.ChatType.PRIVATE,
    ))
    # Buttons that no active conversation owns, e.g. from before a restart.
    application.add_handler(CallbackQueryHandler(handlers.expired_button))
    application.add_error_handler(on_error)
    return application


def main():
    configure_logging()
    build_application().run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
