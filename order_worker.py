"""Lease-based Telegram notification delivery and order expiry."""
import asyncio
from contextlib import suppress
from datetime import timedelta
import logging

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from order_handlers import action_buttons
from orders import as_utc, order_text

LOG = logging.getLogger(__name__)


class OrderWorker:
    def __init__(self, store, bot):
        self.store, self.bot, self.task = store, bot, None

    async def call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def run_once(self, batch_size=20):
        await self.call(self.store.clear_expired_abuse_limits)
        await self.call(self.store.expire_orders)
        for _ in range(batch_size):
            event = await self.call(self.store.claim_notification)
            if event is None:
                break
            order = await self.call(self.store.notification_order, event)
            if order is None:
                await self.call(self.store.finish_notification, event, 'SUPPRESSED')
                continue
            now = await self.call(self.store.order_now)
            if now >= as_utc(event['created_at']) + timedelta(hours=24):
                await self.call(self.store.finish_notification, event, 'FAILED', 'DeliveryWindowExpired')
                continue
            try:
                message = await asyncio.wait_for(self.bot.send_message(
                    chat_id=event['recipient_id'], text=order_text(order, event['recipient_id']),
                    reply_markup=action_buttons(order, event['recipient_id']),
                    connect_timeout=5, read_timeout=10, write_timeout=10, pool_timeout=5), timeout=30)
            except RetryAfter as exc:
                seconds = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else exc.retry_after
                await self.call(self.store.finish_notification, event, 'PENDING', 'RetryAfter', max(1, seconds))
            except (Forbidden, BadRequest) as exc:
                await self.call(self.store.finish_notification, event, 'FAILED', type(exc).__name__)
                LOG.warning('Order notification failed: event_id=%s category=%s', event['event_id'], type(exc).__name__)
            except (NetworkError, TelegramError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
                delay = min(900, 30 * 2 ** min(int(event['attempt_count']) - 1, 5))
                await self.call(self.store.finish_notification, event, 'PENDING', type(exc).__name__, delay)
            except Exception as exc:
                LOG.error('Order send failed: event_id=%s category=%s', event['event_id'], type(exc).__name__)
                await self.call(self.store.finish_notification, event, 'PENDING', type(exc).__name__, 60)
            else:
                await self.call(self.store.finish_notification, event, 'SENT', None, 0, message.message_id)

    async def run(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error('Order worker iteration failed: category=%s', type(exc).__name__)
            await asyncio.sleep(30)

    def start(self):
        # Application.create_task is awaited during Application.stop, before the
        # post_stop hook can cancel this loop. Manage this task explicitly.
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name='smebot-orders')

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
