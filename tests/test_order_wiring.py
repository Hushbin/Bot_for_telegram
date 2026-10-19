"""Ordering journeys through the real Telegram application wiring."""
from types import SimpleNamespace
from test_wiring import WiringTestCase, NAME_EMAIL, PHONE
import main
from order_worker import OrderWorker


class OrderWiringTests(WiringTestCase):
    async def make_app(self, enabled=True):
        app = main.build_application(SimpleNamespace(backend='local', bot_token='123456:TEST-TOKEN',
                                                     orders_enabled=enabled), self.store, self.media)
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        return app

    def seed(self):
        self.seed_seller(2)
        self.store.upsert_user(1, 'Buyer', 'b@example.com', PHONE)
        return self.store.create_product(2, 'Shirt', 'clothing', None, 1999, 5)

    async def draft(self, quantity='2'):
        await self.say(1, '/order 1')
        await self.say(1, quantity)
        return self.app.user_data[1]['order_draft'].copy()

    async def confirm(self, draft, message_id=None):
        query = self._update(callback_query={
            'id': str(self.next_id), 'chat_instance': 'x', 'data': 'confirm:' + draft['key'],
            'from': {'id': 1, 'is_bot': False, 'first_name': 'T'},
            'message': self._message(1, message_id=message_id or draft['message_id'], text='summary')})
        return await self.feed(query)

    async def test_limit_preserves_confirmation_draft_for_retry(self):
        from abuse_limits import AbuseLimits
        from datetime import timedelta
        self.seed()
        self.store.abuse_limits = AbuseLimits(enabled=True)
        draft = await self.draft()
        await self.confirm(draft)
        draft = await self.draft()
        texts = await self.confirm(draft)
        self.assertIn('Please wait', '\n'.join(texts))
        self.assertEqual(self.app.user_data[1]['order_draft'], draft)
        now = self.store.order_now() + timedelta(seconds=30)
        self.store.order_clock = lambda: now
        await self.confirm(draft)
        self.assertEqual(len(self.store.list_orders(1)), 2)

    async def test_purchase_notify_accept_complete(self):
        self.seed()
        draft = await self.draft()
        self.assertIn('Order #1', '\n'.join(await self.confirm(draft)))
        await OrderWorker(self.store, self.app.bot).run_once()
        messages = [p for api, p in self.sent if api == 'sendMessage']
        self.assertTrue(any(p['chat_id'] == 2 and 'Buyer' in p['text'] for p in messages))
        await self.tap(2, 'ord:accept:1:1')
        self.assertEqual(self.store.get_product(1)['stock_quantity'], 3)
        await self.tap(2, 'ord:complete:1:2')
        self.assertEqual(self.store.get_order(1, 1)['status'], 'COMPLETED')
        self.assertIn('Shirt', '\n'.join(await self.say(1, '/orders')))
        self.assertIn('Shirt', '\n'.join(await self.say(2, '/sales')))

    async def test_signup_returns_to_product(self):
        self.seed_seller(2)
        self.store.create_product(2, 'Shirt', 'clothing', None, 1999, 5)
        await self.say(1, '/order 1')
        await self.tap(1, 'role:buyer')
        await self.say(1, NAME_EMAIL)
        self.assertIn('How many', '\n'.join(await self.say(1, PHONE)))
        await self.say(1, '1')
        self.assertIn('order_draft', self.app.user_data[1])

    async def test_confirmation_is_bound_to_summary_message(self):
        self.seed()
        draft = await self.draft()
        self.assertIn('expired', '\n'.join(await self.confirm(draft, 99999)))
        self.assertEqual(self.store.list_orders(1), [])
        await self.confirm(draft)
        await self.confirm(draft)
        self.assertEqual(len(self.store.list_orders(1)), 1)

    async def test_edit_quantity_invalidates_old_summary(self):
        self.seed()
        old = await self.draft()
        mid = self.last_message_id
        await self.say(1, '3', edited=True, message_id=mid)
        new = self.app.user_data[1]['order_draft'].copy()
        self.assertNotEqual(old['key'], new['key'])
        await self.confirm(old)
        self.assertEqual(self.store.list_orders(1), [])
        await self.confirm(new)
        self.assertEqual(self.store.get_order(1, 1)['quantity'], 3)

    async def test_price_change_requires_fresh_summary(self):
        self.seed()
        draft = await self.draft()
        self.store.update_product(2, 1, 'Shirt', 'clothing', None, 2500, 5)
        self.assertIn('new summary', '\n'.join(await self.confirm(draft)))
        self.assertEqual(self.store.list_orders(1), [])
        await self.say(1, '2')
        self.assertEqual(self.app.user_data[1]['order_draft']['quote']['unit_price_minor'], 2500)

    async def test_order_actions_preserve_active_profile_conversation(self):
        self.seed()
        await self.confirm(await self.draft())
        await self.say(2, '/profile')
        await self.tap(2, 'ord:accept:1:1')
        await self.say(2, 'Seller Changed, changed@example.com')
        await self.say(2, PHONE)
        self.assertEqual(self.store.get_user(2)['full_name'], 'Seller Changed')

    async def test_catalog_navigation_preserves_unconfirmed_order(self):
        self.seed()
        await self.say(1, '/order 1')
        texts, answers = await self.tap(1, 'prodpage:all:all:new')
        self.assertEqual(len(answers), 1)
        self.assertEqual(len(texts), 2)
        await self.say(1, '2')
        draft = self.app.user_data[1]['order_draft'].copy()
        await self.tap(1, 'prodpage:all:all:new')
        self.assertEqual(self.app.user_data[1]['order_draft'], draft)
        self.assertEqual(self.store.list_orders(1), [])
        await self.confirm(draft)
        self.assertEqual(self.store.get_order(1, 1)['quantity'], 2)

    async def test_expired_product_entry_button_preserves_an_order_draft(self):
        self.seed()
        self.seed_seller(1)
        await self.say(1, '/addproduct')
        await self.say(1, 'Unsaved hat')
        entry = self.app.user_data[1]['product_draft']
        old_button = f'entry:{entry["token"]}:art'
        draft = await self.draft()
        self.assertNotIn('product_draft', self.app.user_data[1])
        texts, answers = await self.tap(1, old_button)
        self.assertIn('expired', texts[0])
        self.assertEqual(len(answers), 1)
        self.assertEqual(self.app.user_data[1]['order_draft'], draft)
        await self.confirm(draft)
        self.assertEqual(self.store.get_order(1, 1)['quantity'], 2)
        self.assertEqual(self.store.count_products(1), 0)

    async def test_seller_cancellation_requires_confirmation(self):
        self.seed()
        await self.confirm(await self.draft())
        await self.tap(2, 'ord:accept:1:1')
        texts, _ = await self.tap(2, 'ord:askcancel:1:2')
        self.assertIn('restore its stock', '\n'.join(texts))
        self.assertEqual(self.store.get_order(1, 1)['status'], 'ACCEPTED')
        await self.tap(2, 'ord:cancel:1:2')
        self.assertEqual(self.store.get_product(1)['stock_quantity'], 5)

    async def test_disabled_creation_keeps_existing_actions(self):
        self.seed()
        await self.confirm(await self.draft())
        self.app = await self.make_app(False)
        self.assertIn('disabled', '\n'.join(await self.say(1, '/order 1')))
        await self.tap(2, 'ord:accept:1:1')
        self.assertEqual(self.store.get_order(1, 1)['status'], 'ACCEPTED')

    async def test_forged_actor_cannot_view_or_accept(self):
        self.seed()
        await self.confirm(await self.draft())
        texts, _ = await self.tap(3, 'ord:accept:1:1')
        self.assertIn('not found', '\n'.join(texts))
        self.assertEqual(self.store.get_order(1, 1)['status'], 'PENDING')
        self.assertIn('private chat', '\n'.join(await self.say(1, '/order 1', chat_type='group')))

    async def test_lifecycle_starts_and_stops_worker(self):
        await self.app.post_init(self.app)
        await self.app.post_stop(self.app)
        await self.app.post_shutdown(self.app)
