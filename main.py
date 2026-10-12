"""SMEbot entry point. Polling is for local development."""

import logging

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
    PRODUCT_DETAILS, PRODUCT_PHOTO,
)


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

    registration = ConversationHandler(
        entry_points=[
            CommandHandler("start", handlers.start),
            CommandHandler("profile", handlers.profile),
            CommandHandler("sellerprofile", handlers.seller_profile),
            CommandHandler("preferences", handlers.preferences),
        ],
        states={
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.choose)],
            CLASS_STATE: [CallbackQueryHandler(handlers.classer, pattern=r"^role:")],
            SME_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                         handlers.seller_details)],
            CHOOSE_PREF: [CallbackQueryHandler(handlers.preference, pattern=r"^cat:")],
        },
        fallbacks=[CommandHandler("cancel", handlers.cancel)],
        allow_reentry=True,
    )
    catalog = ConversationHandler(
        entry_points=[
            CommandHandler("addproduct", handlers.add_product),
            CommandHandler("photo", handlers.add_photo),
        ],
        states={
            PRODUCT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                             handlers.product_details)],
            PRODUCT_PHOTO: [
                MessageHandler(filters.PHOTO, handlers.photo_for_product),
                CommandHandler("skip", handlers.skip),
            ],
        },
        fallbacks=[CommandHandler("cancel", handlers.cancel)],
        allow_reentry=True,
    )
    application.add_handler(registration)
    application.add_handler(catalog)
    application.add_handler(CommandHandler("products", handlers.products))
    application.add_handler(CommandHandler("product", handlers.product))
    application.add_handler(CommandHandler("myproducts", handlers.my_products))
    application.add_handler(CommandHandler("stock", handlers.stock))
    return application


def main():
    logging.basicConfig(level=logging.INFO)
    build_application().run_polling()


if __name__ == "__main__":
    main()
