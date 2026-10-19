"""Management journeys through the real private-chat conversation dispatcher."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegram import PhotoSize
from telegram.error import NetworkError
from telegram.request import HTTPXRequest

from catalog import CATEGORIES
from test_wiring import WiringTestCase


class ManagementTests(WiringTestCase):
    def button(self, label):
        for message_id, (api, params) in reversed(list(enumerate(self.sent, 1))):
            if api != 'sendMessage':
                continue
            for row in params.get('reply_markup', {}).get('inline_keyboard', []):
                for button in row:
                    if button['text'] == label:
                        self.assertLessEqual(len(button['callback_data'].encode()), 64)
                        return button['callback_data'], message_id
        self.fail(f'No button: {label}')

    async def press(self, button, uid=100, chat_type='private', app=None):
        data, message_id = button
        start = len(self.sent)
        texts = await self.feed(self._update(callback_query={
            'id': str(self.next_id), 'chat_instance': 'x', 'data': data,
            'from': {'id': uid, 'is_bot': False, 'first_name': 'T'},
            'message': self._message(uid, chat_type=chat_type, message_id=message_id, text='prompt')}), app)
        self.assertEqual(sum(api == 'answerCallbackQuery' for api, _ in self.sent[start:]), 1)
        return '\n'.join(texts)

    def seed(self):
        self.seed_seller(100)
        self.pid = self.store.create_product(100, 'Shirt', 'clothing', 'Cotton', 1999, 5)

    async def open(self):
        self.seed()
        return await self.say(100, '/manageproduct 1')

    async def photo(self, raw=b'\xff\xd8\xff\x00', size=4):
        async def download(out):
            out.write(raw)
        with patch.object(PhotoSize, 'get_file', AsyncMock(return_value=SimpleNamespace(
                download_to_memory=download))):
            return await self.feed(self._update(message=self._message(100, photo=[dict(
                file_id='incoming', file_unique_id='unique', width=100, height=100, file_size=size)])))

    async def test_all_fields_require_review_and_preserve_unrelated_updates(self):
        await self.open()
        for label, field, text, value in [('Name', 'name', 'Blue shirt', 'Blue shirt'),
                ('Price', 'price_minor', '29.99', 2999), ('Stock', 'stock_quantity', '8', 8),
                ('Description', 'description', 'Linen', 'Linen')]:
            await self.press(self.button(label))
            original = self.store.get_product(self.pid)[field]
            self.assertIn('Save this change?', '\n'.join(await self.say(100, text)))
            self.assertEqual(self.store.get_product(self.pid)[field], original)
            self.assertIn('updated.', await self.press(self.button('Save')))
            self.assertEqual(self.store.get_product(self.pid)[field], value)
        await self.press(self.button('Category'))
        await self.press(self.button(CATEGORIES['art']))
        await self.press(self.button('Save'))
        self.assertEqual(self.store.get_product(self.pid)['category'], 'art')
        await self.press(self.button('Description'))
        await self.press(self.button('Clear description'))
        await self.press(self.button('Save'))
        self.assertIsNone(self.store.get_product(self.pid)['description'])

    async def test_references_manage_buttons_and_stable_numbers(self):
        self.seed()
        await self.say(100, '/manageproduct')
        self.assertIn('not yours', '\n'.join(await self.say(100, 'G999')))
        self.assertIn('Choose what', '\n'.join(await self.say(100, f'G{self.pid}')))
        await self.say(100, '/myproducts')
        await self.press(self.button('Manage #1'))
        self.assertEqual(self.app.user_data[100]['product_management']['pid'], self.pid)
        await self.say(100, f'/product {self.pid}')
        await self.press(self.button('Manage #1'))
        await self.press(self.button('Remove product'))
        await self.press(self.button('Confirm removal'))
        new = self.store.create_product(100, 'Next', 'art', None, 100, 1)
        await self.say(100, '/myproducts')
        await self.press(self.button('Manage #2'))
        self.assertEqual(self.app.user_data[100]['product_management']['pid'], new)

    async def test_latest_edit_revokes_previous_review_and_invalid_correction(self):
        await self.open()
        await self.press(self.button('Name'))
        await self.say(100, 'First')
        incoming = self.last_message_id
        old_save = self.button('Save')
        await self.say(100, 'Corrected', edited=True, message_id=incoming)
        self.assertIn('expired', await self.press(old_save))
        corrected_save = self.button('Save')
        await self.say(100, ' ', edited=True, message_id=incoming)
        self.assertIn('expired', await self.press(corrected_save))
        self.assertEqual(self.store.get_product(self.pid)['name'], 'Shirt')
        await self.say(100, 'Final')
        await self.press(self.button('Save'))
        await self.say(100, 'Late edit', edited=True, message_id=incoming)
        self.assertEqual(self.store.get_product(self.pid)['name'], 'Final')

    async def test_stale_other_actor_wrong_message_group_and_restart_are_inert(self):
        await self.open()
        await self.press(self.button('Name'))
        await self.say(100, 'Changed')
        save = self.button('Save')
        self.assertIn('expired', await self.press(save, uid=200))
        self.assertIn('expired', await self.press((save[0], save[1] + 999)))
        self.assertIn('private', await self.press(save, chat_type='group'))
        restarted = await self.make_app()
        self.assertIn('expired', await self.press(save, app=restarted))
        await self.say(100, '/addproduct')
        draft = self.app.user_data[100]['product_draft'].copy()
        self.assertIn('expired', await self.press(save))
        self.assertEqual(self.app.user_data[100]['product_draft'], draft)
        self.assertEqual(self.store.get_product(self.pid)['name'], 'Shirt')

    async def test_forged_open_does_not_discard_draft_and_cancel_discards_management(self):
        await self.open()
        self.seed_seller(200)
        other = self.store.create_product(200, 'Other', 'art', None, 100, 1)
        await self.say(100, '/addproduct')
        draft = self.app.user_data[100]['product_draft'].copy()
        self.assertIn('not yours', await self.press((f'pm:open:{other}', 1)))
        self.assertEqual(self.app.user_data[100]['product_draft'], draft)
        await self.say(100, '/manageproduct 1')
        cancel = self.button('Cancel')
        await self.press(cancel)
        self.assertNotIn('product_management', self.app.user_data[100])
        self.assertIn('expired', await self.press(cancel))

    async def test_conflict_requires_fresh_review_and_duplicate_save_is_inert(self):
        await self.open()
        await self.press(self.button('Name'))
        await self.say(100, 'Telegram')
        self.store.edit_seller_product_field(100, self.pid, 'name', 'Panel', 'Shirt')
        self.assertIn('changed', await self.press(self.button('Save')))
        self.assertEqual(self.store.get_product(self.pid)['name'], 'Panel')
        await self.press(self.button('Name'))
        await self.say(100, 'Final')
        save = self.button('Save')
        await self.press(save)
        with patch.object(self.store, 'edit_seller_product_field') as write:
            self.assertIn('expired', await self.press(save))
            write.assert_not_called()

    async def test_remove_confirmation_rechecks_product_and_pending_orders(self):
        await self.open()
        await self.press(self.button('Remove product'))
        confirm = self.button('Confirm removal')
        self.store.edit_seller_product_field(100, self.pid, 'name', 'Changed', 'Shirt')
        self.assertIn('changed', await self.press(confirm))
        await self.press(self.button('Remove product'))
        self.store.upsert_user(200, 'Buyer', 'b@example.com', '+1234567890')
        self.store.create_order(200, self.store.quote_order(200, self.pid, 1), uuid4().hex)
        self.assertIn('/sales', await self.press(self.button('Confirm removal')))
        self.assertIsNotNone(self.store.get_product(self.pid))

    async def test_photo_replacement_skip_invalid_and_quota_preserve_previous(self):
        from abuse_limits import AbuseLimits
        await self.open()
        key = self.seed_photo(100, self.pid)
        await self.say(100, '/manageproduct 1')
        await self.press(self.button('Replace photo'))
        await self.say(100, '/skip')
        self.assertEqual(self.store.get_product(self.pid)['photo_object_key'], key)
        self.store.abuse_limits = AbuseLimits(enabled=True, photos_daily=1)
        await self.press(self.button('Replace photo'))
        await self.photo(b'bad')
        self.assertEqual(self.store.abuse_summary(100)['photo_attempt_count'], 1)
        self.assertEqual(self.store.get_product(self.pid)['photo_object_key'], key)
        self.assertIn('Daily photo', '\n'.join(await self.photo()))
        self.store.abuse_limits = AbuseLimits()
        self.assertIn('Photo saved', '\n'.join(await self.photo()))
        self.assertNotEqual(self.store.get_product(self.pid)['photo_object_key'], key)
        self.assertFalse(self.media._path(key).exists())
        self.assertEqual(self.app.user_data[100]['product_management']['phase'], 'menu')

    async def test_stale_photo_revision_and_oversize_do_not_upload(self):
        await self.open()
        await self.press(self.button('Add photo'))
        key = self.seed_photo(100, self.pid)
        with patch.object(self.media, 'put_product_photo') as put:
            await self.photo(size=5 * 1024 * 1024 + 1)
            await self.photo()
            put.assert_not_called()
        self.assertEqual(self.store.get_product(self.pid)['photo_object_key'], key)

    async def test_remove_deletes_photo_and_repeated_confirmation_is_inert(self):
        await self.open()
        key = self.seed_photo(100, self.pid)
        await self.press(self.button('Remove product'))
        confirm = self.button('Confirm removal')
        self.assertIn('Product removed', await self.press(confirm))
        self.assertIsNone(self.store.get_product(self.pid))
        self.assertFalse(self.media._path(key).exists())
        self.assertIn('expired', await self.press(confirm))

    async def test_lost_success_reply_does_not_allow_duplicate_save(self):
        await self.open()
        await self.press(self.button('Name'))
        await self.say(100, 'Saved')
        save = self.button('Save')
        original = HTTPXRequest.do_request
        async def fail_reply(request, url, method, request_data=None, **kwargs):
            if request_data and request_data.parameters.get('text') == 'Name updated.':
                raise NetworkError('simulated send failure')
            return await original(request, url, method, request_data, **kwargs)
        with patch.object(HTTPXRequest, 'do_request', fail_reply):
            with self.assertLogs('handlers', level='ERROR'):
                await self.press(save)
        self.assertEqual(self.store.get_product(self.pid)['name'], 'Saved')
        with patch.object(self.store, 'edit_seller_product_field') as write:
            self.assertIn('expired', await self.press(save))
            write.assert_not_called()

    async def test_full_description_is_visible_on_removal_review(self):
        await self.open()
        self.store.edit_seller_product_field(100, self.pid, 'description', 'x' * 1000, 'Cotton')
        text = await self.press(self.button('Remove product'))
        self.assertIn('x' * 1000, text)

    async def test_erased_account_cannot_save_a_pending_review(self):
        await self.open()
        await self.press(self.button('Name'))
        await self.say(100, 'Changed')
        self.store.erase_user(100)
        self.assertIn('Register as a seller', await self.press(self.button('Save')))
        self.assertNotIn('product_management', self.app.user_data[100])

    async def test_cancelled_photo_request_finishes_attach_and_cleanup_in_worker(self):
        from product_media import save_product_photo
        self.seed()
        old_key = self.seed_photo(100, self.pid)
        old_version = self.store.get_product(self.pid)['photo_version_id']
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        upload = self.media.put_product_photo
        def blocked_upload(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError('test upload not released')
            return upload(*args)
        def worker():
            try:
                return save_product_photo(self.store, self.media, 100, self.pid,
                                          b'\xff\xd8\xff\x00', old_key, old_version)
            finally:
                finished.set()
        with patch.object(self.media, 'put_product_photo', blocked_upload):
            task = asyncio.create_task(asyncio.to_thread(worker))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 5))
        self.assertNotEqual(self.store.get_product(self.pid)['photo_object_key'], old_key)
        self.assertFalse(self.media._path(old_key).exists())
