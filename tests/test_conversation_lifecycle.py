"""Fake-clock expiry checks through the real Telegram dispatcher and SQLite."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import PhotoSize

import main
from catalog import CATEGORIES
from conversation_lifecycle import FLOW_FIELDS, IDLE_SECONDS
from test_wiring import WiringTestCase, NAME_EMAIL, PHONE, PRODUCT


class LifecycleTests(WiringTestCase):
    async def make_app(self):
        app = main.build_application(SimpleNamespace(backend='local', bot_token='123456:TEST-TOKEN',
                                                     orders_enabled=True), self.store, self.media)
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = 10000
        self.lifecycle = self.app.bot_data['conversation_lifecycle']
        self.lifecycle.clock = lambda: self.now

    def advance(self, seconds=IDLE_SECONDS):
        self.now += seconds

    def button(self, label):
        for mid, (api, params) in reversed(list(enumerate(self.sent, 1))):
            if api == 'sendMessage':
                for row in params.get('reply_markup', {}).get('inline_keyboard', []):
                    for button in row:
                        if button['text'] == label:
                            return button['callback_data'], mid
        self.fail('Missing button: ' + label)

    async def press(self, button, uid=1, app=None):
        data, mid = button
        start = len(self.sent)
        texts = await self.feed(self._update(callback_query={
            'id': str(self.next_id), 'chat_instance': 'x', 'data': data,
            'from': {'id': uid, 'is_bot': False, 'first_name': 'T'},
            'message': self._message(uid, message_id=mid, text='prompt')}), app)
        self.assertEqual(sum(api == 'answerCallbackQuery' for api, _ in self.sent[start:]), 1)
        return '\n'.join(texts)

    def assert_cleared(self, uid=1):
        self.assertFalse(set(FLOW_FIELDS) & self.app.user_data[uid].keys())

    async def guided_review(self):
        self.seed_seller(1)
        await self.say(1, '/addproduct')
        await self.say(1, 'Shirt')
        await self.press(self.button(CATEGORIES['clothing']))
        await self.say(1, '19.99')
        await self.say(1, '5')
        await self.say(1, 'Cotton')
        return self.button('Save product')

    async def order_review(self):
        self.seed_seller(2)
        self.store.create_product(2, 'Shirt', 'clothing', None, 1999, 5)
        self.store.upsert_user(1, 'Buyer', 'b@example.com', PHONE)
        await self.say(1, '/order 1')
        await self.say(1, '2')
        return self.button('Confirm order')

    async def test_exact_deadline_blocks_input_without_waiting_for_sweep(self):
        await self.begin(1)
        self.advance(IDLE_SECONDS - 1)
        self.assertIn('phone number', (await self.say(1, NAME_EMAIL))[0])
        self.advance()
        texts = await self.say(1, PHONE)
        self.assertEqual(len(texts), 1)
        self.assertIn('expired', texts[0])
        self.assertIsNone(self.store.get_user(1))
        self.assert_cleared()
        # END returned by the guard also releases PTB's old state.
        self.assertIn('no active step', (await self.say(1, NAME_EMAIL))[0])
        self.assertIsNone(self.store.get_user(1))

    async def test_sweep_silently_clears_pii_and_removes_keyboard_on_next_interaction(self):
        await self.begin(1)
        await self.say(1, NAME_EMAIL)
        self.app.user_data[1]['unrelated'] = 'keep'
        count = len(self.sent)
        self.advance()
        self.assertEqual(self.lifecycle.expire_due(), 1)
        self.assertEqual(len(self.sent), count)
        self.assert_cleared()
        self.assertEqual(self.app.user_data[1]['unrelated'], 'keep')
        self.assertEqual(self.lifecycle.expire_due(), 0)
        texts = await self.say(1, '/privacy')
        self.assertIn('expired', texts[0])
        notice = [p for api, p in self.sent[count:] if api == 'sendMessage'][0]
        self.assertTrue(notice['reply_markup']['remove_keyboard'])
        self.assertIn('What I store', texts[1])
        self.assertFalse(any('expired' in t for t in await self.say(1, '/privacy')))
        self.assertIn('expired', (await self.say(1, PHONE))[0])
        self.assertIsNone(self.store.get_user(1))

    async def test_users_have_independent_deadlines(self):
        await self.begin(1)
        self.advance(IDLE_SECONDS // 2)
        await self.begin(2)
        self.advance(IDLE_SECONDS // 2)
        self.lifecycle.expire_due()
        self.assert_cleared(1)
        self.assertIn('signup_role', self.app.user_data[2])
        self.assertIn('phone number', (await self.say(2, NAME_EMAIL))[0])

    async def test_invalid_current_input_and_hints_refresh_only_current_flow(self):
        await self.begin(1)
        self.advance(IDLE_SECONDS - 1)
        await self.say(1, 'invalid')
        self.advance(IDLE_SECONDS - 1)
        # Non-text input gets the current step's hint and renews the deadline.
        texts = await self.feed(self._update(message=self._message(1, sticker={
            'file_id': 'x', 'file_unique_id': 'y', 'width': 1, 'height': 1,
            'type': 'regular', 'is_animated': False, 'is_video': False})))
        self.assertIn('full name', texts[0])
        self.advance(IDLE_SECONDS - 1)
        self.assertIn('phone number', (await self.say(1, NAME_EMAIL))[0])

    async def test_read_only_commands_and_catalog_buttons_do_not_refresh_draft(self):
        await self.begin(1)
        self.advance(IDLE_SECONDS - 1)
        for command in ('/products', '/myproducts', '/privacy', '/mydata', '/orders', '/sales'):
            await self.say(1, command)
        await self.tap(1, 'prodpage:all:all:new')
        self.advance(1)
        self.assertIn('expired', (await self.say(1, NAME_EMAIL))[0])
        self.assert_cleared()

    async def test_fresh_commands_and_menu_buttons_replace_expired_flows(self):
        await self.begin(1)
        self.advance()
        texts = await self.say(1, '/start')
        self.assertIn('Welcome to SMEbot', '\n'.join(texts))
        await self.tap(1, 'role:buyer')
        self.advance()
        texts, answers = await self.tap(1, 'menu:start')
        self.assertIn('Welcome to SMEbot', '\n'.join(texts))
        self.assertEqual(len(answers), 1)
        await self.tap(1, 'role:buyer')
        self.assertIn('phone number', (await self.say(1, NAME_EMAIL))[0])

    async def test_cancel_clears_correction_context_without_later_expiry_notice(self):
        await self.register(1)
        await self.say(1, '/cancel')
        self.assertNotIn(1, self.lifecycle.flows)
        self.advance()
        self.assertEqual(self.lifecycle.expire_due(), 0)
        self.assertIn('Choose an action', (await self.say(1, '/menu'))[0])

    async def test_new_generation_is_safe_from_old_deadline(self):
        await self.begin(1)
        self.advance(100)
        await self.say(1, '/start')
        self.advance(IDLE_SECONDS - 100)
        self.assertEqual(self.lifecycle.expire_due(), 0)
        await self.tap(1, 'role:buyer')
        self.assertIn('phone number', (await self.say(1, NAME_EMAIL))[0])

    async def test_expired_guided_save_cannot_create_product(self):
        save = await self.guided_review()
        self.advance()
        self.assertIn('expired', await self.press(save))
        self.assertEqual(self.store.count_products(1), 0)
        self.assert_cleared()
        self.assertIn('no active step', (await self.say(1, PRODUCT))[0])

    async def test_saved_product_survives_pending_photo_expiry(self):
        save = await self.guided_review()
        await self.press(save)
        self.advance()
        self.lifecycle.expire_due()
        self.assert_cleared()
        self.assertEqual(self.store.count_products(1), 1)
        self.assertIn('expired', (await self.say(1, '/skip'))[0])
        self.assertIsNotNone(self.store.get_product(1))

    async def test_contact_correction_expires_without_erasing_saved_profile(self):
        await self.register(1)
        phone_mid = self.last_message_id
        self.assertIn('draft', self.app.user_data[1])
        self.advance()
        self.assertIn('expired', (await self.say(1, '+1999999999', edited=True,
                                              message_id=phone_mid))[0])
        self.assert_cleared()
        self.assertEqual(self.store.get_user(1)['phone'], PHONE)

    async def test_valid_post_save_correction_renews_correction_window(self):
        await self.register(1)
        phone_mid = self.last_message_id
        self.advance(IDLE_SECONDS - 1)
        await self.say(1, '+1999999999', edited=True, message_id=phone_mid)
        self.assertEqual(self.store.get_user(1)['phone'], '+1999999999')
        self.advance(IDLE_SECONDS - 1)
        self.assertEqual(self.lifecycle.expire_due(), 0)
        self.advance(1)
        self.assertEqual(self.lifecycle.expire_due(), 1)
        self.assert_cleared()

    async def test_compact_product_correction_expires_after_photo_skip(self):
        self.seed_seller(1)
        await self.say(1, '/addproduct')
        await self.say(1, PRODUCT)
        mid = self.last_message_id
        await self.say(1, '/skip')
        self.advance()
        await self.say(1, 'Changed | art | 1 | 1 | D', edited=True, message_id=mid)
        self.assertEqual(self.store.get_product(1)['name'], 'Blue shirt')
        self.assert_cleared()

    async def test_unknown_edits_are_silent_and_do_not_renew_or_consume_notice(self):
        await self.begin(1)
        await self.say(1, NAME_EMAIL)
        self.advance(IDLE_SECONDS - 1)
        self.assertEqual(await self.say(1, 'Unknown', edited=True, message_id=99999), [])
        self.advance(1)
        self.assertEqual(await self.say(1, 'Unknown', edited=True, message_id=99999), [])
        self.assert_cleared()
        self.assertIn('expired', (await self.say(1, '/privacy'))[0])

    async def test_expired_management_save_removal_and_photo_selection_are_inert(self):
        self.seed_seller(1)
        pid = self.store.create_product(1, 'Shirt', 'clothing', None, 1999, 5)
        for action, label in (('Name', 'Save'), ('Remove product', 'Confirm removal'),
                              ('Add photo', None)):
            with self.subTest(action=action):
                await self.say(1, '/manageproduct 1')
                await self.press(self.button(action))
                if action == 'Name':
                    await self.say(1, 'Changed')
                button = self.button(label) if label else None
                self.advance()
                if button:
                    self.assertIn('expired', await self.press(button))
                else:
                    with patch.object(PhotoSize, 'get_file', AsyncMock()) as download:
                        await self.feed(self._update(message=self._message(1, photo=[dict(
                            file_id='incoming', file_unique_id='unique', width=1, height=1)])))
                        download.assert_not_awaited()
                product = self.store.get_product(pid)
                self.assertEqual(product['name'], 'Shirt')
                self.assertIsNone(product['photo_object_key'])
                self.assert_cleared()

    async def test_fresh_manage_button_can_open_after_expiry(self):
        self.seed_seller(1)
        pid = self.store.create_product(1, 'Shirt', 'clothing', None, 1999, 5)
        await self.say(1, '/profile')
        self.advance()
        texts, answers = await self.tap(1, f'pm:open:{pid}')
        self.assertIn('Choose what to change', '\n'.join(texts))
        self.assertEqual(len(answers), 1)
        self.assertEqual(self.app.user_data[1]['product_management']['pid'], pid)

    async def test_expired_order_confirmation_does_not_create_order_or_charge_budget(self):
        from abuse_limits import AbuseLimits
        self.store.abuse_limits = AbuseLimits(enabled=True)
        confirm = await self.order_review()
        self.advance()
        self.assertIn('expired', await self.press(confirm))
        self.assertEqual(self.store.list_orders(1), [])
        self.assertEqual(self.store.get_product(1)['stock_quantity'], 5)
        self.assertIsNone(self.store.abuse_summary(1))
        self.assert_cleared()

    async def test_saved_confirmation_replay_survives_expiry_and_restart(self):
        confirm = await self.order_review()
        await self.press(confirm)
        await self.say(1, '/profile')
        self.advance()
        self.assertIn('Order #1', await self.press(confirm))
        self.assert_cleared()
        restarted = await self.make_app()
        self.assertIn('Order #1', await self.press(confirm, app=restarted))
        self.assertEqual(len(self.store.list_orders(1)), 1)

    async def test_durable_actions_survive_expiry_without_renewing_other_flow(self):
        confirm = await self.order_review()
        await self.press(confirm)
        await self.say(2, '/profile')
        self.advance(IDLE_SECONDS - 1)
        texts, answers = await self.tap(2, 'ord:accept:1:1')
        self.assertEqual(len(answers), 1)
        self.assertIn('Accepted', '\n'.join(texts))
        self.advance(1)
        self.assertIn('expired', (await self.say(2, NAME_EMAIL))[0])
        self.assertEqual(self.store.get_order(1, 2)['status'], 'ACCEPTED')
        self.assertEqual(self.store.get_product(1)['stock_quantity'], 3)

    async def test_saved_confirmation_replay_does_not_refresh_new_draft(self):
        confirm = await self.order_review()
        await self.press(confirm)
        await self.say(1, '/profile')
        self.advance(IDLE_SECONDS - 1)
        await self.press(confirm)
        self.advance(1)
        self.assertIn('expired', (await self.say(1, NAME_EMAIL))[0])

    async def test_stale_stateful_entry_does_not_refresh_new_draft(self):
        old_save = await self.guided_review()
        await self.say(1, '/profile')
        self.advance(IDLE_SECONDS - 1)
        self.assertIn('expired', await self.press(old_save))
        self.advance(1)
        self.assertIn('expired', (await self.say(1, NAME_EMAIL))[0])

    async def test_restart_drops_unconfirmed_drafts_and_guides_new_text(self):
        confirm = await self.order_review()
        restarted = await self.make_app()
        self.assertIn('/menu', (await self.say(1, '2', app=restarted))[0])
        self.assertIn('expired', await self.press(confirm, app=restarted))
        self.assertEqual(await self.say(1, '3', edited=True, message_id=99999, app=restarted), [])
        self.assertEqual(self.store.list_orders(1), [])
        self.assertIn('Choose an action', (await self.say(1, '/menu', app=restarted))[0])

    async def test_restart_text_removes_stale_phone_keyboard_and_does_not_save_contact(self):
        await self.begin(1)
        await self.say(1, NAME_EMAIL)
        restarted = await self.make_app()
        self.assertIn('/menu', (await self.say(1, PHONE, app=restarted))[0])
        reply = [p for api, p in self.sent if api == 'sendMessage'][-1]
        self.assertTrue(reply['reply_markup']['remove_keyboard'])
        self.assertIsNone(self.store.get_user(1))

    async def test_busy_save_is_not_expired_and_new_deadline_starts_after_completion(self):
        save = await self.guided_review()
        entered, release = asyncio.Event(), asyncio.Event()
        from handlers import BotHandlers
        original = BotHandlers.run
        async def paused_run(handlers, operation, *args, **kwargs):
            if operation == self.store.create_product:
                entered.set()
                await release.wait()
            return await original(handlers, operation, *args, **kwargs)
        with patch.object(BotHandlers, 'run', paused_run):
            task = asyncio.create_task(self.press(save))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.advance(IDLE_SECONDS * 2)
                self.assertEqual(self.lifecycle.expire_due(), 0)
                self.assertIn('product_draft', self.app.user_data[1])
            finally:
                release.set()
                await task
        self.assertEqual(self.store.count_products(1), 1)
        self.assertIn('pending_product_id', self.app.user_data[1])
        self.assertEqual(self.lifecycle.expire_due(), 0)
        self.advance()
        self.assertEqual(self.lifecycle.expire_due(), 1)
        self.assertEqual(self.store.count_products(1), 1)

    async def busy_photo(self, attach_fails=False):
        from handlers import BotHandlers
        from product_media import save_product_photo
        self.seed_seller(1)
        pid = self.store.create_product(1, 'Shirt', 'clothing', None, 1999, 5)
        await self.say(1, '/photo 1')
        entered, release = asyncio.Event(), asyncio.Event()
        original = BotHandlers.run
        async def paused_run(handlers, operation, *args, **kwargs):
            if operation == save_product_photo:
                entered.set()
                await release.wait()
            return await original(handlers, operation, *args, **kwargs)
        async def download(out):
            out.write(b'\xff\xd8\xff\x00')
        failure = (patch.object(self.store, 'replace_seller_product_photo',
                                side_effect=RuntimeError('test attach failure'))
                   if attach_fails else nullcontext())
        with patch.object(BotHandlers, 'run', paused_run), failure, patch.object(
                PhotoSize, 'get_file', AsyncMock(return_value=SimpleNamespace(
                    download_to_memory=download))), patch.object(
                        self.media, 'put_product_photo', wraps=self.media.put_product_photo) as upload:
            task = asyncio.create_task(self.feed(self._update(message=self._message(1, photo=[dict(
                file_id='incoming', file_unique_id='unique', width=1, height=1, file_size=4)]))))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.advance(IDLE_SECONDS * 2)
                self.assertEqual(self.lifecycle.expire_due(), 0)
                self.assertEqual(self.app.user_data[1]['pending_product_id'], pid)
            finally:
                release.set()
                await task
            upload.assert_called_once()
        if attach_fails:
            self.assertIsNone(self.store.get_product(pid)['photo_object_key'])
            self.assertEqual(list(self.media.directory.rglob('*.jpg')), [])
            self.assertIn('pending_product_id', self.app.user_data[1])
        else:
            key = self.store.get_product(pid)['photo_object_key']
            self.assertTrue(self.media._path(key).exists())
            self.assertNotIn('pending_product_id', self.app.user_data[1])

    async def test_busy_photo_finishes_upload_and_attach(self):
        await self.busy_photo()

    async def test_busy_photo_finishes_compensation_after_attach_failure(self):
        with self.assertLogs('handlers', level='ERROR'):
            await self.busy_photo(attach_fails=True)

    async def test_expired_preferences_and_erasure_confirmation_cannot_write(self):
        self.store.upsert_user(1, 'Buyer', 'b@example.com', PHONE)
        for command, callback in (('/preferences', 'cat:art'), ('/deleteme', 'erase:yes')):
            with self.subTest(command=command):
                await self.say(1, command)
                self.advance()
                texts, answers = await self.tap(1, callback)
                self.assertIn('expired', texts[0])
                self.assertEqual(len(answers), 1)
                user = self.store.get_user(1)
                self.assertEqual(user['full_name'], 'Buyer')
                self.assertIsNone(user['preferred_category'])

    async def test_background_task_clears_drafts_without_further_updates(self):
        await self.begin(1)
        await self.say(1, NAME_EMAIL)
        count = len(self.sent)
        self.advance()
        # Yield between sweeps instead of waiting for a real 30-second timer.
        with patch('conversation_lifecycle.SWEEP_SECONDS', 0):
            self.lifecycle.start()
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assert_cleared()
                self.assertEqual(len(self.sent), count)
            finally:
                await self.lifecycle.stop()

    async def test_runtime_starts_and_stops_sweeper_idempotently(self):
        await self.app.post_init(self.app)
        task = self.lifecycle.task
        self.assertIsNotNone(task)
        self.lifecycle.start()
        self.assertIs(self.lifecycle.task, task)
        await self.app.post_stop(self.app)
        await self.app.post_shutdown(self.app)
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.lifecycle.task)
