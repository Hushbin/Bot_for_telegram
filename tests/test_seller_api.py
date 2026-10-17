"""Real SQLite seller API authorization, authentication and stock mutation tests."""
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4
import httpx
from local_backend import SQLiteStore
from seller_api import COOKIE, SellerSessions, create_api


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


class SellerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SQLiteStore(Path(temp.name) / 'db.sqlite')
        for uid in (1, 2, 3):
            self.store.upsert_user(uid, f'User {uid}', 'u@example.com', '+1234567890')
        for uid in (1, 2):
            self.store.upsert_seller(uid, f'Store {uid}', 's@example.com', 'Online', '+1234567890')
        self.pid = self.store.create_product(1, 'Shirt', 'clothing', 'Cotton', 1999, 5)
        self.other_pid = self.store.create_product(2, 'Other', 'art', '', 100, 5)
        self.sessions = SellerSessions()
        app = create_api(self.store, self.sessions, 'https://seller.test', 'test_bot')
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
