"""Bot-specific, persistent Telegram photo references and bounded delivery retries."""
import asyncio
import io
import logging
from time import monotonic

from telegram.error import BadRequest, NetworkError, TimedOut

LOG = logging.getLogger(__name__)
PHOTO_TIMEOUT = 30
DELAY_NOTICE = (
    "Telegram hasn’t confirmed delivery yet. The photo may still arrive; "
    "please wait before requesting it again."
)


def invalid_photo_reference(error):
    # Only explicit reference failures establish that Telegram did not send a photo.
    return str(error).casefold().startswith((
        "wrong file identifier/http url specified", "wrong remote file identifier specified",
        "file reference expired", "file_reference_expired",
    ))


class ProductPhotoDelivery:
    def __init__(self, store, media):
        self.store = store
        self.media = media

    async def remember(self, product, bot_id, file_id, update_id=None):
        try:
            await asyncio.to_thread(
                self.store.cache_product_photo, product['product_id'],
                product['photo_object_key'], product['photo_version_id'], bot_id, file_id)
        except Exception as exc:
            # Database exception text may contain bind values, including references.
            LOG.warning('Photo cache write failed update_id=%s product_id=%s error_type=%s',
                        update_id, product['product_id'], type(exc).__name__)

    async def delay_notice(self, update, pid, error):
        update_id = getattr(update, 'update_id', None)
        LOG.warning('Photo delivery unconfirmed update_id=%s product_id=%s error_type=%s',
                    update_id, pid, type(error).__name__)
        try:
            await update.effective_message.reply_text(DELAY_NOTICE)
        except Exception as exc:
            LOG.warning('Photo delay notice failed update_id=%s product_id=%s error_type=%s',
                        update_id, pid, type(exc).__name__)

    async def send(self, update, bot_id, product, details, *, lookup_ms=0):
        update_id = getattr(update, 'update_id', None)
        pid = product['product_id']
        cached = (product['telegram_photo_file_id']
                  if product['telegram_photo_bot_id'] == bot_id else None)
        LOG.info('Photo cache %s update_id=%s product_id=%s lookup_ms=%.1f',
                 'hit' if cached else 'miss', update_id, pid, lookup_ms)
        # At most one cached send followed by one upload after explicit rejection.
        for attempt in range(2):
            caption, options = details(product)
            if not product['photo_object_key']:
                await update.effective_message.reply_text(caption, **options)
                return
            if cached:
                photo = cached
            else:
                started = monotonic()
                try:
                    raw = await asyncio.to_thread(self.media.get_photo, product['photo_object_key'])
                finally:
                    LOG.info('Photo storage download update_id=%s product_id=%s elapsed_ms=%.1f',
                             update_id, pid, (monotonic() - started) * 1000)
                photo = io.BytesIO(raw)
            started = monotonic()
            source = 'cache' if cached else 'upload'
            outcome = 'failed'
            try:
                try:
                    sent = await update.effective_message.reply_photo(
                        photo=photo, caption=caption, read_timeout=PHOTO_TIMEOUT,
                        write_timeout=PHOTO_TIMEOUT, **options)
                finally:
                    send_ms = (monotonic() - started) * 1000
                outcome = 'confirmed'
            except BadRequest as exc:
                if not cached or attempt or not invalid_photo_reference(exc):
                    raise
                outcome = 'reference_rejected'
                try:
                    await asyncio.to_thread(
                        self.store.clear_product_photo_cache, pid, product['photo_object_key'],
                        product['photo_version_id'], bot_id, cached)
                except Exception as clear_error:
                    LOG.warning('Photo cache clear failed update_id=%s product_id=%s error_type=%s',
                                update_id, pid, type(clear_error).__name__)
                product = await asyncio.to_thread(self.store.get_product, pid)
                if not product:
                    await update.effective_message.reply_text('Product not found.')
                    return
                cached = None
                continue
            except TimedOut as exc:
                outcome = 'timeout'
                await self.delay_notice(update, pid, exc)
                return
            except NetworkError as exc:
                # A dropped connection can also occur after delivery.
                outcome = 'connection_failure'
                await self.delay_notice(update, pid, exc)
                return
            finally:
                LOG.info('Photo Telegram send update_id=%s product_id=%s source=%s '
                         'outcome=%s elapsed_ms=%.1f', update_id, pid,
                         source, outcome, send_ms)
            if not cached and sent.photo:
                largest = max(sent.photo, key=lambda size: size.width * size.height)
                await self.remember(product, bot_id, largest.file_id, update_id)
            return
