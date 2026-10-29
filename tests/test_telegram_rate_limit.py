"""Fake-clock admission and real-dispatcher checks without network or credentials."""
from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import PhotoSize
from telegram.ext import TypeHandler

import main
from abuse_limits import AbuseLimits
from config import load_settings
from conversation_lifecycle import IDLE_SECONDS
from telegram_rate_limit import RequestLimiter, TelegramRateLimit
from test_wiring import WiringTestCase, NAME_EMAIL, PHONE, PRODUCT


class PolicyTests(unittest.TestCase):
    def test_defaults_overrides_and_settings_wiring(self):
        self.assertEqual(TelegramRateLimit.from_environment({}), TelegramRateLimit())
        env = dict(TELEGRAM_RATE_LIMIT_ENABLED='TRUE', TELEGRAM_RATE_LIMIT_BURST='20',
                   TELEGRAM_RATE_LIMIT_REFILL_PER_SECOND='2')
        expected = TelegramRateLimit(True, 20, 2)
        self.assertEqual(TelegramRateLimit.from_environment(env), expected)
        with patch.dict('os.environ', dict(env, APP_BACKEND='local', BOT_TOKEN='fixture'), clear=True):
            self.assertEqual(load_settings().telegram_rate_limit, expected)

    def test_invalid_configuration_even_when_disabled(self):
        for suffix, values in (
            ('ENABLED', ['yes', '', '1', ' false']),
            ('BURST', ['0', '-1', '1.5', '1001', '', '١', ' 10', '+10']),
            ('REFILL_PER_SECOND', ['0', '-1', '1.5', '1001', '', '١'])):
            for value in values:
                with self.subTest(suffix=suffix, value=value), self.assertRaises(ValueError):
                    TelegramRateLimit.from_environment({'TELEGRAM_RATE_LIMIT_' + suffix: value})
        for value in ('1', '1000'):
            self.assertEqual(TelegramRateLimit.from_environment({
                'TELEGRAM_RATE_LIMIT_BURST': value}).burst, int(value))


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.limiter = RequestLimiter(TelegramRateLimit(enabled=True), clock=lambda: self.now)

    def exhaust(self, uid=1):
        for _ in range(10):
            self.assertTrue(self.limiter.admit(uid).allowed)

    def test_burst_fractional_refill_exact_boundary_and_ceiling(self):
        self.exhaust()
        self.assertFalse(self.limiter.admit(1).allowed)
        self.now += .5
        decision = self.limiter.admit(1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_after, 1)
        self.now += .5
        self.assertTrue(self.limiter.admit(1).allowed)
        self.assertFalse(self.limiter.admit(1).allowed)
        self.now += 20
        self.exhaust()
        self.assertFalse(self.limiter.admit(1).allowed)

    def test_custom_refill_and_user_isolation(self):
        self.limiter = RequestLimiter(TelegramRateLimit(True, 2, 2), clock=lambda: self.now)
        for uid in (1, 2):
            self.assertTrue(self.limiter.admit(uid).allowed)
            self.assertTrue(self.limiter.admit(uid).allowed)
            self.assertFalse(self.limiter.admit(uid).allowed)
        self.now += .5
        self.assertTrue(self.limiter.admit(1).allowed)
        self.assertTrue(self.limiter.admit(2).allowed)

    def test_recovery_reserve_refill_and_normal_tokens_first(self):
        self.assertTrue(self.limiter.admit(1, recovery=True).allowed)
        self.assertEqual(self.limiter.users[1].reserve_seconds, 30)
        for _ in range(9):
            self.limiter.admit(1)
        for _ in range(3):
            self.assertTrue(self.limiter.admit(1, recovery=True).allowed)
        self.assertFalse(self.limiter.admit(1, recovery=True).allowed)

    def test_frequent_requests_do_not_round_reserve_refill_below_boundary(self):
        self.exhaust()
        for _ in range(3):
            self.limiter.admit(1, recovery=True)
        for step in range(1, 101):
            self.now = 1000.0 + step / 10
            self.limiter.admit(1)
        self.assertTrue(self.limiter.admit(1, recovery=True).allowed)
        self.now += 10
        self.exhaust()
        self.assertTrue(self.limiter.admit(1, recovery=True).allowed)
        self.assertFalse(self.limiter.admit(1, recovery=True).allowed)

    def test_feedback_shared_across_actions_and_rejections_do_not_delay_refill(self):
        self.exhaust()
        self.assertTrue(self.limiter.admit(1).feedback)
        self.assertFalse(self.limiter.admit(1, recovery=True).feedback)  # admitted reserve
        self.assertFalse(self.limiter.admit(1).feedback)
        self.now += 1
        self.assertTrue(self.limiter.admit(1).allowed)
        self.assertFalse(self.limiter.admit(1).feedback)
        self.now += 9
        for _ in range(9):
            self.limiter.admit(1)
        self.assertTrue(self.limiter.admit(1).feedback)

    def test_silent_rejection_does_not_consume_private_feedback(self):
        self.exhaust()
        self.assertFalse(self.limiter.admit(1, feedback=False).feedback)
        self.assertTrue(self.limiter.admit(1).feedback)

    def test_capacity_rejects_new_users_without_eviction_and_expires_oldest(self):
        with patch('telegram_rate_limit.MAX_USERS', 2):
            self.exhaust(1)
            self.now += 1
            self.exhaust(2)
            decision = self.limiter.admit(3, recovery=True)
            self.assertFalse(decision.allowed)
            self.assertFalse(decision.feedback)
            self.assertEqual(list(self.limiter.users), [1, 2])
            self.now += 59
            self.assertTrue(self.limiter.admit(3).allowed)
            self.assertEqual(list(self.limiter.users), [2, 3])

    def test_rejected_activity_refreshes_order_without_resetting_budgets(self):
        self.exhaust(1)
        self.exhaust(2)
        self.assertFalse(self.limiter.admit(1).allowed)
        self.assertEqual(list(self.limiter.users), [2, 1])
        self.assertEqual(self.limiter.users[1].tokens, 0)

    def test_expiry_waits_for_custom_bucket_to_refill(self):
        self.limiter = RequestLimiter(TelegramRateLimit(True, 1000, 1), clock=lambda: self.now)
        for _ in range(1000):
            self.limiter.admit(1)
        self.now += 60
        self.limiter.admit(2)
        self.assertIn(1, self.limiter.users)
        self.now += 940
        self.limiter.admit(2)
        self.assertNotIn(1, self.limiter.users)

    def test_disabled_keeps_no_user_state_or_counts_and_restart_resets(self):
        disabled = RequestLimiter(TelegramRateLimit(), clock=lambda: self.now)
        for _ in range(20):
            self.assertTrue(disabled.admit(1).allowed)
        self.assertEqual(len(disabled.users), 0)
        self.assertFalse(any(disabled.counts.values()))
        self.exhaust()
        restarted = RequestLimiter(self.limiter.policy, clock=lambda: self.now)
        self.assertTrue(restarted.admit(1).allowed)

    def test_aggregate_logs_are_bounded_and_contain_only_counts(self):
        with self.assertLogs('telegram_rate_limit', 'INFO') as logs:
            self.exhaust(987654321)
            self.limiter.admit(987654321)
            self.limiter.admit(987654321, recovery=True)
            self.now += 60
            self.limiter.admit(2)
            self.limiter.admit(2)
        self.assertEqual(len(logs.output), 1)
        payload = json.loads(logs.output[0].split('TELEGRAM_RATE_LIMIT ')[1])
        self.assertEqual(payload, dict(v=1, accepted=12, rejected=1, reserve_used=1,
                                       capacity_rejected=0))
        self.assertNotIn('987654321', logs.output[0])
        self.assertEqual(self.limiter.counts['accepted'], 1)

    def test_logging_failure_cannot_change_admission(self):
        self.now += 60
        with patch('telegram_rate_limit.LOG.info', side_effect=RuntimeError('fixture')):
            self.assertTrue(self.limiter.admit(1).allowed)


class GateWiringTests(WiringTestCase):
    async def make_app(self):
        app = main.build_application(SimpleNamespace(
            backend='local', bot_token='123456:TEST-TOKEN', orders_enabled=True,
            telegram_rate_limit=TelegramRateLimit(enabled=True)), self.store, self.media)
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = 1000.0
        self.limiter = self.app.bot_data['telegram_request_limiter']
        self.limiter.clock = lambda: self.now
        self.limiter.reported_at = self.now
        self.lifecycle = self.app.bot_data['conversation_lifecycle']
        self.lifecycle.clock = lambda: self.now

    def exhaust(self, uid=1, reserve=False):
        while self.limiter.admit(uid).allowed:
            pass
        if reserve:
            while self.limiter.admit(uid, recovery=True).allowed:
                pass
        # Budget setup should not suppress the feedback being tested.
        self.limiter.users[uid].last_feedback = float('-inf')

    async def test_real_gate_rejects_eleventh_and_recovers_without_affecting_other_user(self):
        for _ in range(10):
            self.assertNotIn('Too many requests', '\n'.join(await self.say(1, '/products')))
        with patch.object(self.store, 'connect', side_effect=AssertionError('database touched')):
            self.assertIn('Too many requests', '\n'.join(await self.say(1, '/products')))
        self.assertNotIn('Too many requests', '\n'.join(await self.say(2, '/products')))
        self.now += 1
        self.assertNotIn('Too many requests', '\n'.join(await self.say(1, '/products')))

    async def test_commands_edits_and_buttons_preserve_draft_and_lifecycle(self):
        await self.begin(1)
        await self.say(1, NAME_EMAIL)
        mid = self.last_message_id
        data = deepcopy(self.app.user_data[1])
        deadline = self.lifecycle.flows[1].deadline
        self.exhaust()
        with patch.object(self.store, 'connect', side_effect=AssertionError('database touched')):
            await self.say(1, '/addproduct')
            await self.say(1, 'Changed, changed@example.invalid', edited=True, message_id=mid)
            texts, answers = await self.tap(1, 'ord:view:1:0')
        self.assertEqual(len(answers), 1)
        self.assertEqual(texts, [])
        self.assertEqual(self.app.user_data[1], data)
        self.assertEqual(self.lifecycle.flows[1].deadline, deadline)
        self.now += 1
        await self.say(1, 'Changed, changed@example.invalid', edited=True, message_id=mid)
        self.assertEqual(self.app.user_data[1]['draft']['name'], 'Changed')

    async def test_rejected_photo_does_not_download_store_or_consume_ledger(self):
        self.seed_seller(1)
        await self.say(1, '/addproduct')
        await self.say(1, PRODUCT)
        data = deepcopy(self.app.user_data[1])
        ledger = self.store.abuse_summary(1)
        self.exhaust()
        with patch.object(PhotoSize, 'get_file', AsyncMock()) as download, \
             patch.object(self.media, 'put_product_photo') as put, \
             patch.object(self.store, 'connect', side_effect=AssertionError('database touched')):
            await self.feed(self._update(message=self._message(1, photo=[dict(
                file_id='fixture', file_unique_id='fixture', width=1, height=1, file_size=100)])))
        download.assert_not_called()
        put.assert_not_called()
        self.assertEqual(self.app.user_data[1], data)
        self.assertEqual(self.store.abuse_summary(1), ledger)

    async def test_feedback_is_shared_and_callback_always_acknowledged_once(self):
        self.exhaust()
        texts, answers = await self.tap(1, 'menu:products')
        self.assertEqual(texts, [])
        self.assertEqual(len(answers), 1)
        self.assertIn('Try again in 1 seconds', answers[0]['text'])
        texts, answers = await self.tap(1, 'menu:products')
        self.assertEqual(texts, [])
        self.assertEqual(len(answers), 1)
        self.assertFalse(answers[0].get('text'))
        self.assertEqual(await self.say(1, '/products'), [])
        self.now += 10
        self.exhaust()
        self.assertIn('Too many requests', '\n'.join(await self.say(1, '/products')))

    async def test_feedback_failure_still_blocks_all_downstream_handlers(self):
        self.exhaust()
        error = AsyncMock()
        self.app.add_error_handler(error)
        downstream = AsyncMock()
        self.app.add_handler(TypeHandler(main.Update, downstream), group=1)
        for callback in (False, True):
            self.limiter.users[1].last_feedback = float('-inf')
            with patch.object(self.app.bot, '_post', AsyncMock(side_effect=RuntimeError('fixture'))):
                if callback:
                    await self.tap(1, 'ord:view:1:0')
                else:
                    await self.say(1, '/products')
        error.assert_not_called()
        downstream.assert_not_called()

    async def test_recovery_commands_and_erasure_have_bounded_separate_allowance(self):
        self.seed_seller(1)
        self.exhaust()
        self.assertIn('What I store', '\n'.join(await self.say(1, '/privacy@test_bot')))
        self.assertNotIn('Too many requests', '\n'.join(await self.say(1, '/mydata')))
        self.assertNotIn('Too many requests', '\n'.join(await self.say(1, '/cancel')))
        self.assertIn('Too many requests', '\n'.join(await self.say(1, '/deleteme')))
        self.now += 10
        self.exhaust()
        self.assertIn('Continue?', '\n'.join(await self.say(1, '/deleteme')))
        # Reserve has been spent, so the confirmation waits without erasing data.
        texts, answers = await self.tap(1, 'erase:yes')
        self.assertEqual(len(answers), 1)
        self.assertIsNone(self.store.get_user(1).get('deleted_at'))
        self.now += 10
        self.exhaust()
        texts, answers = await self.tap(1, 'erase:yes')
        self.assertIn('Your data has been erased', '\n'.join(texts))
        self.assertEqual(len(answers), 1)

    async def test_only_exact_recovery_commands_and_callbacks_use_reserve(self):
        self.exhaust()
        for command in ('/cancelled', '/privacy@other_bot'):
            await self.say(1, command)
        for data in ('erase:yes:extra', 'entry:fake:cancel', 'ord:cancel:1:1'):
            await self.tap(1, data)
        self.assertEqual(self.limiter.users[1].reserve_seconds, 30)
        await self.say(1, '/cancel', edited=True)
        self.assertEqual(self.limiter.users[1].reserve_seconds, 30)
        await self.tap(1, 'erase:no')
        self.assertEqual(self.limiter.users[1].reserve_seconds, 20)

    async def test_group_traffic_only_counts_recognized_commands_and_callbacks(self):
        await self.say(1, 'unrelated chat', chat_type='group')
        await self.say(1, '/products@other_bot', chat_type='group')
        await self.say(1, '/products', edited=True, chat_type='group')
        self.assertNotIn(1, self.limiter.users)
        await self.say(1, '/products@test_bot', chat_type='group')
        self.assertEqual(self.limiter.users[1].tokens, 9)
        self.exhaust()
        self.assertEqual(await self.say(1, '/cancel', chat_type='group'), [])
        self.assertEqual(self.limiter.users[1].reserve_seconds, 30)
        query = self._update(callback_query={
            'id': 'group', 'chat_instance': 'x', 'data': 'erase:yes',
            'from': {'id': 1, 'is_bot': False, 'first_name': 'T'},
            'message': self._message(1, chat_type='group', text='button')})
        start = len(self.sent)
        await self.feed(query)
        self.assertEqual(sum(api == 'answerCallbackQuery' for api, _ in self.sent[start:]), 1)
        self.assertEqual(self.limiter.users[1].reserve_seconds, 30)

    async def test_capacity_gives_no_notice_but_acknowledges_buttons(self):
        with patch('telegram_rate_limit.MAX_USERS', 1):
            self.exhaust(2)
            self.assertEqual(await self.say(1, '/privacy'), [])
            texts, answers = await self.tap(1, 'erase:yes')
        self.assertEqual(texts, [])
        self.assertEqual(len(answers), 1)
        self.assertFalse(answers[0].get('text'))
        self.assertNotIn(1, self.limiter.users)

    async def test_missing_user_and_unsupported_update_do_not_allocate_buckets(self):
        message = self._message(1, text='anonymous')
        del message['from']
        # Missing senders are outside the gate's policy; test the guard directly
        # without invoking unrelated handlers that require a private-chat user.
        await self.app.handlers[-3][0].callback(self._update(message=message), None)
        await self.feed(self._update())
        self.assertEqual(len(self.limiter.users), 0)

    async def test_rejected_updates_do_not_prolong_normal_draft_expiry(self):
        await self.begin(1)
        await self.say(1, NAME_EMAIL)
        deadline = self.lifecycle.flows[1].deadline
        self.now += IDLE_SECONDS - 1
        self.exhaust()
        await self.say(1, PHONE)
        self.assertEqual(self.lifecycle.flows[1].deadline, deadline)
        self.now += 1
        self.assertEqual(self.lifecycle.expire_due(), 1)
        self.assertNotIn('draft', self.app.user_data[1])
        self.assertIsNone(self.store.get_user(1))

    async def test_order_confirmation_retry_is_idempotent_and_preserves_ledger(self):
        self.seed_seller(2)
        self.store.upsert_user(1, 'Buyer', 'b@example.invalid', PHONE)
        self.store.create_product(2, 'Shirt', 'clothing', None, 1999, 5)
        self.store.abuse_limits = AbuseLimits(enabled=True)
        await self.say(1, '/order 1')
        await self.say(1, '2')
        draft = deepcopy(self.app.user_data[1]['order_draft'])
        ledger = self.store.abuse_summary(1)

        async def confirm():
            return await self.feed(self._update(callback_query={
                'id': str(self.next_id), 'chat_instance': 'x', 'data': 'confirm:' + draft['key'],
                'from': {'id': 1, 'is_bot': False, 'first_name': 'T'},
                'message': self._message(1, message_id=draft['message_id'], text='summary')}))

        self.exhaust()
        with patch.object(self.store, 'connect', side_effect=AssertionError('database touched')):
            await confirm()
        self.assertEqual(self.app.user_data[1]['order_draft'], draft)
        self.assertEqual(self.store.abuse_summary(1), ledger)
        self.assertEqual(self.store.list_orders(1), [])
        self.now += 1
        self.assertIn('Order #1', '\n'.join(await confirm()))
        self.now += 1
        await confirm()
        self.assertEqual(len(self.store.list_orders(1)), 1)
        self.assertEqual(self.store.abuse_summary(1)['order_count'], 1)

    async def test_disabled_application_preserves_existing_dispatch(self):
        app = main.build_application(SimpleNamespace(backend='local', bot_token='123456:TEST-TOKEN'),
                                     self.store, self.media)
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        for _ in range(12):
            self.assertNotIn('Too many requests', '\n'.join(await self.say(1, '/privacy', app=app)))
        self.assertEqual(len(app.bot_data['telegram_request_limiter'].users), 0)
