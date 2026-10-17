"""Durable delivery retries, lease ownership, and shutdown without Telegram access."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from telegram.error import Forbidden, NetworkError, RetryAfter
from local_backend import SQLiteStore
from order_worker import OrderWorker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteStore(Path(directory.name) / 'orders.db')
        self.now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.store.order_clock = lambda: self.now
        for uid in (1, 2, 3):
            self.store.upsert_user(uid, 'Jane', 'j@example.com', '+1234567890')
        self.store.upsert_seller(2, 'Shop', 's@example.com', 'Online', '+1234567890')
        self.pid = self.store.create_product(2, 'Shirt', 'clothing', None, 1999, 5)
        self.order = self.store.create_order(1, self.store.quote_order(1, self.pid, 1), uuid4().hex)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))
        self.worker = OrderWorker(self.store, self.bot)
        self.addAsyncCleanup(self.worker.stop)

    def event(self):
        with self.store.order_transaction() as db:
            return db.one('SELECT * FROM order_notifications ORDER BY event_id')

    async def test_success_records_delivery(self):
        await self.worker.run_once()
        self.assertEqual((self.event()['state'], self.event()['message_id']), ('SENT', 123))
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 2)
        await self.worker.run_once()
        self.bot.send_message.assert_awaited_once()

    async def test_network_retry_survives_worker_restart(self):
        self.bot.send_message.side_effect = NetworkError('offline')
        await self.worker.run_once()
        self.assertEqual(self.event()['state'], 'PENDING')
        self.assertEqual(self.event()['next_attempt_at'] - self.now, timedelta(seconds=30))
        self.now += timedelta(seconds=30)
        self.bot.send_message.side_effect = None
        await OrderWorker(self.store, self.bot).run_once()
        self.assertEqual(self.event()['state'], 'SENT')

    async def test_timeout_retries(self):
        self.bot.send_message.side_effect = TimeoutError()
        await self.worker.run_once()
        self.assertEqual(self.event()['last_error'], 'TimeoutError')
        self.assertEqual(self.event()['state'], 'PENDING')

    async def test_telegram_retry_after_is_honored(self):
        self.bot.send_message.side_effect = RetryAfter(120)
        await self.worker.run_once()
        self.assertEqual(self.event()['next_attempt_at'] - self.now, timedelta(seconds=120))

    async def test_blocked_recipient_failure_is_visible(self):
        self.bot.send_message.side_effect = Forbidden('blocked')
        await self.worker.run_once()
        self.assertEqual(self.event()['state'], 'FAILED')
        self.assertTrue(self.store.get_order(self.order['order_id'], 1)['delivery_failed'])

    async def test_old_status_event_is_suppressed(self):
        self.store.change_order(self.order['order_id'], 2, 'accept', 1)
        await self.worker.run_once()
        self.assertEqual(self.event()['state'], 'SUPPRESSED')
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 1)

    async def test_erasure_invalidates_leased_event(self):
        event = self.store.claim_notification()
        self.store.erase_user(1)
        self.assertIsNone(self.store.notification_order(event))
        self.assertFalse(self.store.finish_notification(event, 'SENT'))

    async def test_reclaimed_lease_rejects_old_ack(self):
        old = self.store.claim_notification()
        self.now += timedelta(seconds=91)
        new = self.store.claim_notification()
        self.assertNotEqual(old['lease_token'], new['lease_token'])
        self.assertFalse(self.store.finish_notification(old, 'SENT'))
        self.assertTrue(self.store.finish_notification(new, 'SENT'))

    async def test_crash_after_send_can_duplicate_message_without_duplicate_order(self):
        event = self.store.claim_notification()
        await self.bot.send_message(chat_id=2, text='sent before crash')
        self.now += timedelta(seconds=91)
        await self.worker.run_once()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(len(self.store.list_orders(1)), 1)
        self.assertEqual(self.event()['state'], 'SENT')

    async def test_downtime_expires_pending_orders(self):
        self.now += timedelta(hours=25)
        await self.worker.run_once()
        self.assertEqual(self.store.get_order(self.order['order_id'], 1)['status'], 'EXPIRED')
        self.assertEqual(self.event()['state'], 'SUPPRESSED')

    async def test_accepted_event_delivery_window_is_bounded(self):
        self.store.change_order(self.order['order_id'], 2, 'accept', 1)
        self.now += timedelta(hours=25)
        await self.worker.run_once()
        with self.store.order_transaction() as db:
            last = db.one('SELECT * FROM order_notifications ORDER BY event_id DESC')
        self.assertEqual((last['state'], last['last_error']), ('FAILED', 'DeliveryWindowExpired'))
        self.bot.send_message.assert_not_awaited()

    async def test_start_stop_are_repeatable(self):
        self.worker.start()
        task = self.worker.task
        self.worker.start()
        self.assertIs(self.worker.task, task)
        await asyncio.sleep(0)
        await self.worker.stop()
        await self.worker.stop()
        self.assertTrue(task.done())
        self.assertIsNone(self.worker.task)
