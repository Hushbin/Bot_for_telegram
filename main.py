"""SMEbot entry point. Uses long polling, both locally and on the OCI VM."""

import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import load_settings
from handlers import (
    BotHandlers, CHOOSING, CLASS_STATE, SME_DETAILS, CHOOSE_PREF,
    PRODUCT_DETAILS, PRODUCT_PHOTO, PHONE, CONFIRM_ERASE, on_error,
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
    "start", "profile", "sellerprofile", "preferences", "addproduct", "photo",
    "products", "product", "myproducts", "stock", "privacy", "mydata", "deleteme",
)


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
    settings = settings or load_settings()
    if store is None or media is None:
        if settings.backend == "local":
            from local_backend import LocalMediaStorage, SQLiteStore
            store = store or SQLiteStore(settings.local_db_path)
            media = media or LocalMediaStorage(settings.local_media_dir)
        else:
            from database import OracleStore
            from storage import MediaStorage
            store = store or OracleStore(settings)
            media = media or MediaStorage(settings)
    store.check()
    handlers = BotHandlers(store, media)
    application = ApplicationBuilder().token(settings.bot_token).concurrent_updates(False).build()

    # One conversation for every multi-step flow. A user can only be in one state at a
    # time, any entry command replaces the current state, and one /cancel clears it.
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", handlers.start, filters=PRIVATE_COMMAND),
            CommandHandler("profile", handlers.profile, filters=PRIVATE_COMMAND),
            CommandHandler("sellerprofile", handlers.seller_profile, filters=PRIVATE_COMMAND),
            CommandHandler("preferences", handlers.preferences, filters=PRIVATE_COMMAND),
            CommandHandler("addproduct", handlers.add_product, filters=PRIVATE_COMMAND),
            CommandHandler("photo", handlers.add_photo, filters=PRIVATE_COMMAND),
            CommandHandler("deleteme", handlers.delete_me, filters=PRIVATE_COMMAND),
        ],
        states={
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
                *with_hint(handlers, "Please tap Seller or Customer in the message above."),
            ],
            SME_DETAILS: [
                MessageHandler(PRIVATE_TEXT, handlers.seller_details),
                *with_hint(handlers, "Please send business name, email, address and phone "
                                     "separated by commas."),
            ],
            CHOOSE_PREF: [
                CallbackQueryHandler(handlers.preference, pattern=r"^cat:"),
                *with_hint(handlers, "Please tap a category in the message above."),
            ],
            PRODUCT_DETAILS: [
                MessageHandler(PRIVATE_TEXT, handlers.product_details),
                *with_hint(handlers, "Please send: name | category | price | stock | description."),
            ],
            PRODUCT_PHOTO: [
                MessageHandler(PRIVATE_COMMAND & filters.PHOTO, handlers.photo_for_product),
                CommandHandler("skip", handlers.skip, filters=PRIVATE_COMMAND),
                *with_hint(handlers, "Please send a photo (not a file), or /skip."),
            ],
        },
        fallbacks=[CommandHandler("cancel", handlers.cancel, filters=PRIVATE_COMMAND)],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    for name, callback in (
        ("products", handlers.products),
        ("product", handlers.product),
        ("myproducts", handlers.my_products),
        ("stock", handlers.stock),
        ("privacy", handlers.privacy),
        ("mydata", handlers.my_data),
    ):
        application.add_handler(CommandHandler(name, callback, filters=PRIVATE_COMMAND))
    # Corrections to a message whose flow has already finished, e.g. the business details.
    application.add_handler(MessageHandler(PRIVATE_EDITED_TEXT, handlers.edited))
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
