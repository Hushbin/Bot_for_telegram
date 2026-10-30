"""Real SQLite seller API authorization, authentication and stock mutation tests."""
import tempfile
import io
from pathlib import Path
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace
import httpx
from PIL import Image
from local_backend import SQLiteStore, LocalMediaStorage
from product_photos import prepare_photo, MAX_PHOTO_BYTES
from seller_api import COOKIE, SellerRuntime, SellerSessions, create_api


def image_bytes(format='PNG', size=(32, 24), **options):
    output = io.BytesIO()
    Image.new('RGB', size, 'orange').save(output, format=format, **options)
    return output.getvalue()


class SessionTests(unittest.TestCase):
    def test_one_use_expiry_and_revocation(self):
        now = [100]
        sessions = SellerSessions(lambda: now[0])
        old = sessions.issue(1)
        token = sessions.issue(1)
        self.assertIsNone(sessions.exchange(old))
        session = sessions.exchange(token)
        self.assertEqual(sessions.user(session), 1)
        self.assertIsNone(sessions.exchange(token))
        sessions.revoke(session)
        self.assertIsNone(sessions.user(session))
        token = sessions.issue(1)
        now[0] += 301
        self.assertIsNone(sessions.exchange(token))
        session = sessions.exchange(sessions.issue(1))
        now[0] += 28801
        self.assertIsNone(sessions.user(session))


class PanelLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_link_is_copyable_and_one_use_without_inline_button(self):
        for origin in ('https://localhost:8443', 'https://127.0.0.1:8443', 'https://[::1]:8443'):
            with self.subTest(origin=origin):
                runtime = SellerRuntime(Mock(get_seller=Mock(return_value={'id': 1}),
                                             get_user=Mock(return_value={'id': 1})), origin, 8081, True)
                reply = AsyncMock()
                update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                         effective_message=SimpleNamespace(reply_text=reply))
                await runtime.panel(update, None)
                text = reply.call_args.args[0]
                self.assertIn(origin + '/#login=', text)
                self.assertNotIn('reply_markup', reply.call_args.kwargs)
                self.assertTrue(reply.call_args.kwargs['link_preview_options'].is_disabled)
                token = text.split('#login=')[1]
                self.assertIsNotNone(runtime.sessions.exchange(token))
                self.assertIsNone(runtime.sessions.exchange(token))

    async def test_public_origin_keeps_sign_in_button(self):
        runtime = SellerRuntime(Mock(get_seller=Mock(return_value={'id': 1}),
                                     get_user=Mock(return_value={'id': 1})), 'https://seller.test', 8081, True)
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 effective_message=SimpleNamespace(reply_text=reply))
        await runtime.panel(update, None)
        button = reply.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertTrue(button.url.startswith('https://seller.test/#login='))


class SellerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SQLiteStore(Path(temp.name) / 'db.sqlite')
        self.media = LocalMediaStorage(Path(temp.name) / 'media')
        for uid in (1, 2, 3):
            self.store.upsert_user(uid, f'User {uid}', 'u@example.com', '+1234567890')
        for uid in (1, 2):
            self.store.upsert_seller(uid, f'Store {uid}', 's@example.com', 'Online', '+1234567890')
        self.pid = self.store.create_product(1, 'Shirt', 'clothing', 'Cotton', 1999, 5)
        self.other_pid = self.store.create_product(2, 'Other', 'art', '', 100, 5)
        self.sessions = SellerSessions()
        app = create_api(self.store, self.sessions, 'https://seller.test', 'test_bot', media=self.media)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='https://seller.test', headers={'Origin': 'https://seller.test'})
        self.addAsyncCleanup(self.client.aclose)
        self.data = dict(name='Changed', category='art', description='', price_minor=29,
                         stock_quantity=6, expected_stock=5)
        await self.login()

    async def login(self, uid=1):
        response = await self.client.post('/api/session', json={'token': self.sessions.issue(uid)})
        self.assertEqual(response.status_code, 200)
        return response

    def order(self, pid=None):
        return self.store.create_order(3, self.store.quote_order(3, pid or self.pid, 2), uuid4().hex)

    async def test_cookie_and_anonymous_access(self):
        response = await self.login()
        for flag in ('HttpOnly', 'Secure', 'SameSite=strict', '__Host-smebot'):
            self.assertIn(flag, response.headers['set-cookie'])
        self.client.cookies.clear()
        self.assertEqual((await self.client.get('/api/state')).status_code, 401)
        self.assertEqual((await self.client.get('/api/health')).json(), {'status': 'ok'})

    async def test_csrf_json_and_size_limits(self):
        response = await self.client.post('/api/products', json=self.data, headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await self.client.post('/api/products', content='{}')).status_code, 403)
        self.assertEqual((await self.client.post('/api/products', json=[])).status_code, 400)
        self.assertEqual((await self.client.post('/api/products', json={'name': 'x'*17000})).status_code, 413)
        self.assertEqual(len(self.store.list_products(seller_id=1)), 1)

    async def test_state_is_scoped_and_not_cached(self):
        own = self.order()
        self.order(self.other_pid)
        response = await self.client.get('/api/state')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        state = response.json()
        self.assertEqual([p['id'] for p in state['products']], [self.pid])
        self.assertEqual([o['id'] for o in state['orders']], [own['order_id']])
        self.assertEqual(state['orders'][0]['customer']['phone'], '+1234567890')

    async def test_product_create_edit_remove_and_validation(self):
        response = await self.client.post('/api/products', json=dict(self.data, seller_id=2))
        self.assertEqual(response.status_code, 200)
        created = response.json()['products'][-1]
        self.assertEqual((created['seller_id'], created['price_minor']), (1, 29))
        self.assertEqual((await self.client.put(f'/api/products/{self.pid}', json=self.data)).status_code, 200)
        self.assertEqual(self.store.get_product(self.pid)['name'], 'Changed')
        response = await self.client.request('DELETE', f'/api/products/{self.pid}', json={})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_product(self.pid))
        for key, value in [('stock_quantity', True), ('price_minor', -1), ('category', 'missing'), ('name', '')]:
            response = await self.client.post('/api/products', json=dict(self.data, **{key: value}))
            self.assertEqual(response.status_code, 400)

    async def test_edits_cannot_overwrite_order_reservations(self):
        order = self.order()
        self.store.change_order(order['order_id'], 1, 'accept', 1)
        self.assertEqual((await self.client.put(f'/api/products/{self.pid}', json=self.data)).status_code, 409)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 3)
        response = await self.client.request('DELETE', f'/api/products/{self.pid}', json={})
        self.assertEqual(response.status_code, 409)

    async def test_order_stock_and_notifications(self):
        order = self.order()
        url = f'/api/orders/{order["order_id"]}'
        response = await self.client.post(url, json={'action': 'accept', 'version': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['orders'][0]['status'], 'accepted')
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 3)
        self.assertEqual((await self.client.post(url, json={'action': 'accept', 'version': 1})).status_code, 409)
        self.assertEqual((await self.client.post(url, json={'action': 'cancel', 'version': 2})).status_code, 200)
        self.assertEqual(self.store.get_product(self.pid)['stock_quantity'], 5)
        with self.store.order_transaction() as db:
            self.assertEqual(len(db.all('SELECT * FROM order_notifications WHERE order_id = :oid', oid=order['order_id'])), 3)

    async def test_other_seller_data_is_inaccessible(self):
        order = self.order(self.other_pid)
        response = await self.client.post(f'/api/orders/{order["order_id"]}', json={'action': 'accept', 'version': 1})
        self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.client.put(f'/api/products/{self.other_pid}', json=self.data)).status_code, 404)
        response = await self.client.request('DELETE', f'/api/products/{self.other_pid}', json={})
        self.assertEqual(response.status_code, 404)

    async def test_erasure_and_non_seller_revoke_access(self):
        self.store.erase_user(1)
        self.assertEqual((await self.client.get('/api/state')).status_code, 403)
        self.assertEqual((await self.client.post('/api/products', json=self.data)).status_code, 403)
        await self.login(3)
        self.assertEqual((await self.client.get('/api/state')).status_code, 403)

    async def test_profile_and_logout(self):
        data = dict(business_name='New store', business_email='n@example.com', address='Town', business_phone='+123456789')
        self.assertEqual((await self.client.put('/api/profile', json=data)).status_code, 200)
        self.assertEqual(self.store.get_seller(1)['business_name'], 'New store')
        self.assertEqual(self.store.get_seller(2)['business_name'], 'Store 2')
        old = self.client.cookies.get(COOKIE)
        self.assertEqual((await self.client.post('/api/logout', json={})).status_code, 200)
        self.assertIsNone(self.sessions.user(old))
        self.assertEqual((await self.client.get('/api/state')).status_code, 401)

    async def upload(self, raw=None, revision='none', pid=None, **headers):
        return await self.client.put(f'/api/products/{pid or self.pid}/photo',
                                     content=image_bytes() if raw is None else raw,
                                     headers={'Content-Type': 'image/png', 'If-Match': revision, **headers})

    async def test_pending_removal_conflict_and_expired_pending_does_not_block(self):
        from datetime import timedelta
        order = self.order()
        response = await self.client.request('DELETE', f'/api/products/{self.pid}', json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn('/sales', response.json()['error'])
        self.store.order_clock = lambda: order['expires_at'] + timedelta(seconds=1)
        self.assertEqual((await self.client.request('DELETE', f'/api/products/{self.pid}', json={})).status_code, 200)
        self.assertEqual(self.store.get_order(order['order_id'], 3)['status'], 'PENDING')

    async def test_removal_deletes_exact_photo_and_retries_failed_cleanup(self):
        await self.upload()
        product = self.store.get_product(self.pid)
        key, version = product['photo_object_key'], product['photo_version_id']
        with patch.object(self.media, 'delete_photo', side_effect=RuntimeError('offline')):
            with self.assertLogs('product_media', level='ERROR'):
                response = await self.client.request('DELETE', f'/api/products/{self.pid}', json={})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_product(self.pid))
        self.assertTrue(self.media._path(key).exists())
        with patch.object(self.media, 'delete_photo', wraps=self.media.delete_photo) as delete:
            response = await self.client.request('DELETE', f'/api/products/{self.pid}', json={})
            self.assertEqual(response.status_code, 200)
            delete.assert_called_once_with(key, version)
        self.assertFalse(self.media._path(key).exists())

    async def test_listing_quota_returns_stable_code_and_keeps_edits_available(self):
        from abuse_limits import AbuseLimits
        self.store.abuse_limits = AbuseLimits(enabled=True, listings_daily=1)
        response = await self.client.post('/api/products', json=self.data)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 'listing_daily')
        self.assertGreater(int(response.headers['retry-after']), 0)
        self.assertEqual((await self.client.put(f'/api/products/{self.pid}', json=self.data)).status_code, 200)
        self.store.abuse_limits = AbuseLimits(enabled=True, active_listings=1)
        # Cooldown has elapsed before testing the independent capacity response.
        from datetime import timedelta
        now = self.store.order_now() + timedelta(seconds=10)
        self.store.order_clock = lambda: now
        response = await self.client.post('/api/products', json=self.data)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'active_listings')
        self.assertNotIn('retry-after', response.headers)

    async def test_upload_denials_do_not_consume_and_admitted_failure_does(self):
        from abuse_limits import AbuseLimits
        self.store.abuse_limits = AbuseLimits(enabled=True, photos_daily=1)
        self.assertEqual((await self.upload(pid=self.other_pid)).status_code, 404)
        self.assertEqual((await self.upload(revision='stale')).status_code, 409)
        self.assertEqual((await self.upload(**{'Content-Length': str(MAX_PHOTO_BYTES + 1)})).status_code, 413)
        self.assertEqual(self.store.abuse_summary(1)['photo_attempt_count'], 0)
        self.assertEqual((await self.upload(b'not an image')).status_code, 400)
        self.assertEqual(self.store.abuse_summary(1)['photo_attempt_count'], 1)
        with patch.object(self.media, 'put_product_photo') as upload:
            response = await self.upload()
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()['code'], 'photo_daily')
            upload.assert_not_called()
        self.assertIsNone(self.store.get_product(self.pid)['photo_object_key'])

    async def test_upload_is_shared_with_bot_and_authenticated_preview(self):
        response = await self.upload()
        self.assertEqual(response.status_code, 200)
        product = response.json()['products'][0]
        self.assertEqual(product['photo_url'], f'/api/products/{self.pid}/photo')
        self.assertNotEqual(product['photo_revision'], 'none')
        self.assertNotIn('photo_object_key', product)
        preview = await self.client.get(product['photo_url'])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers['content-type'], 'image/jpeg')
        self.assertEqual(preview.headers['cache-control'], 'no-store')
        stored = self.store.get_product(self.pid)
        self.assertEqual(preview.content, self.media.get_photo(stored['photo_object_key']))
        with Image.open(io.BytesIO(preview.content)) as image:
            self.assertEqual(image.size, (32, 24))
        self.client.cookies.clear()
        self.assertEqual((await self.client.get(product['photo_url'])).status_code, 401)
        self.assertEqual((await self.upload()).status_code, 401)

    async def test_panel_photo_caches_on_first_telegram_view_and_replacement_clears_it(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from handlers import BotHandlers
        self.assertEqual((await self.upload()).status_code, 200)
        self.assertIsNone(self.store.get_product(self.pid)['telegram_photo_file_id'])
        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock(
            return_value=SimpleNamespace(photo=[SimpleNamespace(
                width=32, height=24, file_id='telegram-reference')])))
        update = SimpleNamespace(update_id=12, effective_message=message,
                                 effective_user=SimpleNamespace(id=1))
        context = SimpleNamespace(args=[str(self.pid)], bot=SimpleNamespace(id=7))
        handler = BotHandlers(self.store, self.media)
        with patch.object(self.media, 'get_photo', wraps=self.media.get_photo) as download:
            await handler.product(update, context)
            await handler.product(update, context)
            download.assert_called_once()
        self.assertIsInstance(message.reply_photo.call_args_list[0].kwargs['photo'], io.BytesIO)
        self.assertEqual(message.reply_photo.call_args_list[1].kwargs['photo'], 'telegram-reference')
        state = (await self.client.get('/api/state')).text
        self.assertNotIn('telegram-reference', state)
        self.assertNotIn('telegram_photo_', state)
        revision = (await self.client.get('/api/state')).json()['products'][0]['photo_revision']
        self.assertEqual((await self.upload(revision=revision)).status_code, 200)
        self.assertIsNone(self.store.get_product(self.pid)['telegram_photo_file_id'])
        self.assertIsNone(self.store.get_product(self.pid)['telegram_photo_bot_id'])

    async def test_replace_cleans_old_version_and_rejects_stale_upload(self):
        first = await self.upload()
        revision = first.json()['products'][0]['photo_revision']
        old_key = self.store.get_product(self.pid)['photo_object_key']
        second = await self.upload(image_bytes('JPEG'), revision=revision, **{'Content-Type': 'image/jpeg'})
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.json()['products'][0]['photo_revision'], revision)
        self.assertFalse(self.media._path(old_key).exists())
        with patch.object(self.media, 'put_product_photo') as put:
            self.assertEqual((await self.upload(revision=revision)).status_code, 409)
            put.assert_not_called()

    async def test_photo_ownership_erasure_and_origin(self):
        self.assertEqual((await self.upload(pid=self.other_pid)).status_code, 404)
        self.assertEqual((await self.client.get(f'/api/products/{self.other_pid}/photo')).status_code, 404)
        self.assertEqual((await self.upload(Origin='https://evil.test')).status_code, 403)
        self.assertEqual((await self.upload(**{'Content-Type': 'text/html'})).status_code, 403)
        self.assertEqual((await self.client.get(f'/api/products/{self.pid}/photo')).status_code, 404)
        self.store.erase_user(1)
        self.assertEqual((await self.upload()).status_code, 403)

    async def test_invalid_oversized_and_disguised_files_are_not_stored(self):
        for raw in (b'', b'\x89PNG\r\n\x1a\n', b'<svg></svg>', image_bytes('GIF'), image_bytes(size=(4001, 4000))):
            response = await self.upload(raw)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.upload(b'x' * (MAX_PHOTO_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertIsNone(self.store.get_product(self.pid)['photo_object_key'])
        self.assertEqual(list(self.media.directory.rglob('*.jpg')), [])

    async def test_failed_database_attach_removes_uploaded_object(self):
        with patch.object(self.store, 'replace_seller_product_photo', side_effect=RuntimeError('db failed')):
            with self.assertLogs('seller_api', level='ERROR'):
                response = await self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.abuse_summary(1)['photo_attempt_count'], 1)
        self.assertIsNone(self.store.get_product(self.pid)['photo_object_key'])
        self.assertEqual(list(self.media.directory.rglob('*.jpg')), [])

    async def test_erasure_during_upload_cannot_attach_or_leave_orphan(self):
        put = self.media.put_product_photo
        def erase_after_upload(*args):
            result = put(*args)
            self.store.erase_user(1)
            return result
        with patch.object(self.media, 'put_product_photo', side_effect=erase_after_upload):
            self.assertEqual((await self.upload()).status_code, 403)
        self.assertEqual(list(self.media.directory.rglob('*.jpg')), [])

    async def test_concurrent_photo_change_preserves_winner_and_cleans_loser(self):
        winner_key, winner_version = self.media.put_product_photo(1, self.pid, image_bytes('JPEG'))
        put = self.media.put_product_photo
        def change_during_upload(*args):
            result = put(*args)
            self.store.set_product_photo(1, self.pid, winner_key, winner_version)
            return result
        with patch.object(self.media, 'put_product_photo', side_effect=change_during_upload):
            self.assertEqual((await self.upload()).status_code, 409)
        self.assertEqual(self.store.get_product(self.pid)['photo_object_key'], winner_key)
        self.assertEqual(len(list(self.media.directory.rglob('*.jpg'))), 1)

    async def test_pending_telegram_upload_cannot_overwrite_panel_photo(self):
        await self.upload()
        key = self.store.get_product(self.pid)['photo_object_key']
        with self.assertRaises(LookupError):
            self.store.set_product_photo(1, self.pid, 'late-telegram.jpg', 'version')
        self.assertEqual(self.store.get_product(self.pid)['photo_object_key'], key)


class PhotoValidationTests(unittest.TestCase):
    def test_orientation_resize_and_private_metadata_removal(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = 'Private author'
        raw = image_bytes('JPEG', (3000, 1000), exif=exif)
        prepared = prepare_photo(raw)
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertLess(image.width, image.height)
            self.assertLessEqual(max(image.size), 2048)
            self.assertFalse(image.getexif())
