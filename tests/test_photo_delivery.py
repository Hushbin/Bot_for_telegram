"""Photo delivery behavior with real SQLite and simulated Telegram responses."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.request import HTTPXRequest

import main
from fakes import FakeMedia
from handlers import BotHandlers
from local_backend import SQLiteStore
from photo_delivery import DELAY_NOTICE


def sent_photo():
    return SimpleNamespace(photo=[
        SimpleNamespace(width=1000, height=800, file_id='largest-reference'),
        SimpleNamespace(width=100, height=80, file_id='small-reference'),
    ])


class PhotoDeliveryFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'db.sqlite'
        self.store = SQLiteStore(self.path)
        self.store.upsert_user(42, 'Seller', 's@example.invalid', '+1234567890')
        self.store.upsert_seller(42, 'Shop', 's@example.invalid', 'Online', '+1234567890')
        self.pid = self.store.create_product(42, 'Shirt', 'clothing', 'Cotton', 1999, 5)
        self.store.set_product_photo(42, self.pid, 'photo.jpg', 'v1')
        self.media = FakeMedia()
        self.media.get_photo = Mock(return_value=b'photo bytes')
        self.message = SimpleNamespace(reply_photo=AsyncMock(return_value=sent_photo()),
                                       reply_text=AsyncMock())
        self.update = SimpleNamespace(update_id=123, effective_message=self.message,
                                      effective_user=SimpleNamespace(id=42))
        self.context = SimpleNamespace(args=[str(self.pid)], bot=SimpleNamespace(id=7))

    async def view(self):
        await BotHandlers(self.store, self.media).product(self.update, self.context)

    def seed(self, reference='cached-reference'):
        return self.store.cache_product_photo(self.pid, 'photo.jpg', 'v1', 7, reference)


class PhotoDeliveryTests(PhotoDeliveryFixture):
    async def test_first_upload_then_reference_after_reopening_database(self):
        await self.view()
        first = self.message.reply_photo.call_args.kwargs
        self.assertIsInstance(first['photo'], io.BytesIO)
        self.assertEqual(first['photo'].getvalue(), b'photo bytes')
        self.assertEqual(self.store.get_product(self.pid)['telegram_photo_file_id'],
                         'largest-reference')
        self.store = SQLiteStore(self.path)
        self.store.update_product(42, self.pid, 'Fresh shirt', 'clothing', 'New details', 500, 3)
        await self.view()
        second = self.message.reply_photo.call_args.kwargs
        self.assertEqual(second['photo'], 'largest-reference')
        self.assertIn('Fresh shirt', second['caption'])
        self.assertIn('5.00', second['caption'])
        self.media.get_photo.assert_called_once_with('photo.jpg')
        for call in self.message.reply_photo.call_args_list:
            self.assertEqual((call.kwargs['read_timeout'], call.kwargs['write_timeout']), (30, 30))
        self.message.reply_text.assert_not_called()

    async def test_different_bot_does_not_use_reference(self):
        self.seed()
        self.context.bot.id = 8
        await self.view()
        self.media.get_photo.assert_called_once()
        self.assertIsInstance(self.message.reply_photo.call_args.kwargs['photo'], io.BytesIO)
        self.assertEqual(self.store.get_product(self.pid)['telegram_photo_bot_id'], 8)

    async def test_cached_photo_rebuilds_order_buttons_from_live_stock(self):
        self.seed()
        self.update.effective_user.id = 99
        handler = BotHandlers(self.store, self.media, orders_enabled=True)
        await handler.product(self.update, self.context)
        self.assertIn('reply_markup', self.message.reply_photo.call_args.kwargs)
        self.store.set_stock(42, self.pid, 0)
        await handler.product(self.update, self.context)
        self.assertNotIn('reply_markup', self.message.reply_photo.call_args.kwargs)
        self.assertEqual(self.message.reply_photo.call_args.kwargs['photo'], 'cached-reference')
        self.media.get_photo.assert_not_called()

    async def test_explicit_invalid_reference_reloads_details_and_uploads_once(self):
        self.seed()

        async def respond(**kwargs):
            if isinstance(kwargs['photo'], str):
                self.store.replace_seller_product_photo(42, self.pid, 'replacement.jpg', 'v2', 'photo.jpg')
                self.store.update_product(42, self.pid, 'Replacement', 'art', '', 100, 2)
                raise BadRequest('Wrong file identifier/HTTP URL specified')
            return sent_photo()

        self.message.reply_photo.side_effect = respond
        await self.view()
        self.assertEqual(self.message.reply_photo.call_count, 2)
        self.media.get_photo.assert_called_once_with('replacement.jpg')
        self.assertIn('Replacement', self.message.reply_photo.call_args.kwargs['caption'])
        self.assertEqual(self.store.get_product(self.pid)['telegram_photo_file_id'], 'largest-reference')

    async def test_recognized_reference_errors_fallback_only_once(self):
        for error in ('Wrong remote file identifier specified: bad', 'File reference expired',
                      'FILE_REFERENCE_EXPIRED'):
            with self.subTest(error=error):
                self.seed()
                self.message.reply_photo.reset_mock()
                self.media.get_photo.reset_mock()
                self.message.reply_photo.side_effect = BadRequest(error)
                await self.view()
                self.assertEqual(self.message.reply_photo.call_count, 2)
                self.media.get_photo.assert_called_once()
                self.assertIsNone(self.store.get_product(self.pid)['telegram_photo_file_id'])

    async def test_unrelated_telegram_errors_do_not_reupload_or_clear_cache(self):
        for error in (BadRequest('Chat not found'), BadRequest('Photo_invalid_dimensions')):
            with self.subTest(error=error):
                self.seed()
                self.message.reply_photo.reset_mock()
                self.message.reply_photo.side_effect = error
                await self.view()
                self.assertEqual(self.message.reply_photo.call_count, 1)
                self.media.get_photo.assert_not_called()
                self.assertEqual(self.store.get_product(self.pid)['telegram_photo_file_id'], 'cached-reference')

    async def test_timeout_or_connection_loss_never_resends_and_preserves_cache(self):
        for cached in (False, True):
            for error in (TimedOut(), NetworkError('connection lost')):
                with self.subTest(cached=cached, error=error):
                    if cached:
                        self.seed()
                    self.message.reply_photo.reset_mock()
                    self.message.reply_text.reset_mock()
                    self.message.reply_photo.side_effect = error
                    await self.view()
                    self.assertEqual(self.message.reply_photo.call_count, 1)
                    self.message.reply_text.assert_awaited_once_with(DELAY_NOTICE)
                    self.assertEqual(self.store.get_product(self.pid)['telegram_photo_file_id'],
                                     'cached-reference' if cached else None)

    async def test_notice_failure_is_logged_without_generic_error(self):
        self.message.reply_photo.side_effect = TimedOut()
        self.message.reply_text.side_effect = NetworkError('failed notice')
        with self.assertLogs('photo_delivery', 'WARNING') as logs:
            await self.view()
        self.message.reply_text.assert_awaited_once_with(DELAY_NOTICE)
        self.assertIn('Photo delay notice failed', '\n'.join(logs.output))

    async def test_cache_failure_after_delivery_is_not_an_outage(self):
        with patch.object(self.store, 'cache_product_photo', side_effect=RuntimeError('private bind')):
            with self.assertLogs('photo_delivery', 'WARNING') as logs:
                await self.view()
        self.message.reply_text.assert_not_called()
        self.assertNotIn('private bind', '\n'.join(logs.output))

    async def test_storage_failure_does_not_claim_uncertain_telegram_delivery(self):
        self.media.get_photo.side_effect = RuntimeError('storage unavailable')
        await self.view()
        self.message.reply_photo.assert_not_called()
        self.message.reply_text.assert_awaited_once_with('The service is unavailable. Please try again later.')

    async def test_inflight_old_send_cannot_cache_replacement(self):
        async def respond(**kwargs):
            self.store.replace_seller_product_photo(42, self.pid, 'photo.jpg', 'v2', 'photo.jpg')
            return sent_photo()
        self.message.reply_photo.side_effect = respond
        await self.view()
        self.assertIsNone(self.store.get_product(self.pid)['telegram_photo_file_id'])

    async def test_telegram_upload_seeds_reference(self):
        pid = self.store.create_product(42, 'New', 'art', '', 100, 1)
        incoming = SimpleNamespace(file_size=4, file_id='incoming-reference')
        async def download(out):
            out.write(b'data')
        incoming.get_file = AsyncMock(return_value=SimpleNamespace(download_to_memory=download))
        self.message.photo = [incoming]
        self.context.user_data = dict(pending_product_id=pid, pending_product_number=2)
        await BotHandlers(self.store, self.media).photo_for_product(self.update, self.context)
        self.assertEqual(self.store.get_product(pid)['telegram_photo_file_id'], 'incoming-reference')
        self.context.args = [str(pid)]
        await self.view()
        self.assertEqual(self.message.reply_photo.call_args.kwargs['photo'], 'incoming-reference')
        self.media.get_photo.assert_not_called()

    async def test_timing_logs_correlate_without_photo_references(self):
        with self.assertLogs('photo_delivery', 'INFO') as logs:
            await self.view()
            await self.view()
        output = '\n'.join(logs.output)
        for marker in ('cache miss', 'cache hit', 'storage download', 'Telegram send',
                       'update_id=123', f'product_id={self.pid}', 'elapsed_ms='):
            self.assertIn(marker, output)
        for private in ('largest-reference', 'photo.jpg', 'photo bytes'):
            self.assertNotIn(private, output)


class PhotoHTTPTests(PhotoDeliveryFixture):
    """Use the real PTB -> HTTPX path; replace only the HTTP transport."""

    async def exercise_http(self, elapsed, cached=False, lost=False):
        calls = []
        if cached:
            self.seed()
        async def respond(request):
            api = request.url.path.rsplit('/', 1)[-1]
            body = await request.aread()
            calls.append((api, body, request.extensions['timeout']))
            if api == 'getMe':
                result = dict(id=7, is_bot=True, first_name='Test', username='test_bot')
            elif api == 'sendPhoto':
                # Simulate time passage against actual HTTPX timeout budgets, without
                # making the offline suite wait 31 seconds or contact Telegram.
                budget = request.extensions['timeout']
                self.assertEqual((budget['read'], budget['write']), (30, 30))
                if lost:
                    raise httpx.ConnectError('connection lost', request=request)
                if elapsed > budget['read']:
                    raise httpx.ReadTimeout('delayed response', request=request)
                result = dict(message_id=11, date=0, chat=dict(id=42, type='private'),
                              photo=[dict(file_id='http-reference', file_unique_id='unique',
                                          width=500, height=500)])
            else:
                result = dict(message_id=12, date=0, chat=dict(id=42, type='private'))
            return httpx.Response(200, json=dict(ok=True, result=result))

        transport = httpx.MockTransport(respond)
        request = HTTPXRequest(httpx_kwargs={'transport': transport})
        settings = SimpleNamespace(backend='local', bot_token='123456:TEST-TOKEN')
        # Build two applications from the same database, confirming startup/reuse.
        for index in range(2 if elapsed < 30 and not lost else 1):
            app = main.build_application(settings, SQLiteStore(self.path), self.media)
            # Application builder uses an ExtBot; install the real HTTPX request
            # before initialize, leaving getUpdates unused.
            original = app.bot._request
            app.bot._request = (original[0], request)
            try:
                await app.initialize()
                update = Update.de_json(dict(update_id=100 + index, message=dict(
                    message_id=100 + index, date=0, chat=dict(id=42, type='private'),
                    **{'from': dict(id=42, is_bot=False, first_name='Seller')},
                    text=f'/product {self.pid}', entities=[dict(type='bot_command', offset=0, length=8)])), app.bot)
                await app.process_update(update)
            finally:
                await app.shutdown()
            if index == 0 and elapsed < 30 and not lost:
                request = HTTPXRequest(httpx_kwargs={'transport': httpx.MockTransport(respond)})
        return calls

    async def test_delayed_over_five_seconds_and_rebuilt_application_use_cache(self):
        calls = await self.exercise_http(6)
        photos = [body for api, body, _ in calls if api == 'sendPhoto']
        self.assertEqual(len(photos), 2)
        self.assertIn(b'photo bytes', photos[0])
        self.assertIn(b'http-reference', photos[1])
        self.assertNotIn(b'photo bytes', photos[1])
        self.media.get_photo.assert_called_once()
        self.assertFalse(any(api == 'sendMessage' for api, _, _ in calls))

    async def test_exhausted_http_timeout_notice_only_and_no_retry(self):
        calls = await self.exercise_http(31, cached=True)
        self.assertEqual(sum(api == 'sendPhoto' for api, _, _ in calls), 1)
        notices = [body.decode() for api, body, _ in calls if api == 'sendMessage']
        from urllib.parse import parse_qs
        self.assertEqual([parse_qs(body)['text'][0] for body in notices], [DELAY_NOTICE])
        self.media.get_photo.assert_not_called()
        self.assertEqual(self.store.get_product(self.pid)['telegram_photo_file_id'], 'cached-reference')

    async def test_http_connection_loss_does_not_retry(self):
        calls = await self.exercise_http(0, lost=True)
        self.assertEqual(sum(api == 'sendPhoto' for api, _, _ in calls), 1)
        self.assertEqual(sum(api == 'sendMessage' for api, _, _ in calls), 1)
