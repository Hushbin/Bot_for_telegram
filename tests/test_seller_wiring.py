"""Synthetic Telegram commands exercise the actual seller sign-in routing."""
from types import SimpleNamespace

import main
from test_wiring import WiringTestCase


class SellerWiringTests(WiringTestCase):
    async def make_app(self):
        settings = SimpleNamespace(backend='local', bot_token='123456:TEST-TOKEN',
                                   orders_enabled=True, seller_panel_url='https://seller.test',
                                   seller_api_port=8081)
        app = main.build_application(settings, self.store, self.media)
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        return app

    async def test_panel_and_deep_link_issue_private_links(self):
        self.seed_seller(2)
        for command in ('/panel', '/start seller_panel'):
            replies = await self.say(2, command)
            self.assertIn('5 minutes', replies[-1])
            message = [p for api, p in self.sent if api == 'sendMessage'][-1]
            url = message['reply_markup']['inline_keyboard'][0][0]['url']
            self.assertTrue(url.startswith('https://seller.test/#login='))
            self.assertGreater(len(url.split('=')[-1]), 40)

    async def test_non_seller_and_group_cannot_get_links(self):
        self.assertIn('/sellerprofile', (await self.say(3, '/panel'))[-1])
        self.seed_seller(2)
        self.assertEqual(await self.say(2, '/panel', chat_type='group'),
                         ['Please message me in a private chat.'])

    async def test_panel_deep_link_clears_abandoned_product_flow(self):
        self.seed_seller(2)
        await self.say(2, '/addproduct')
        await self.say(2, 'Shirt | clothing | 1.00 | 5 | Cotton')
        previous = self.last_message_id
        await self.say(2, '/start seller_panel')
        self.assertNotIn('last_input', self.app.user_data[2])
        self.assertEqual(await self.say(2, 'Shirt | clothing | 9.00 | 5 | Cotton',
                                        edited=True, message_id=previous), [])
        self.assertEqual(self.store.get_product(1)['price_minor'], 100)
