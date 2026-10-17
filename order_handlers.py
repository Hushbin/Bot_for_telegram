"""Private Telegram order conversations and durable order actions."""
import logging
import re
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, ConversationHandler
from orders import OrderError, STATUS_LABELS, order_text, positive_int, summary_text

LOG = logging.getLogger(__name__)
ORDER_ID, ORDER_QUANTITY, ORDER_CONFIRM = range(20, 23)


def action_buttons(order, actor_id):
    oid, version = order['order_id'], order['version']
    actions = []
    if order['seller_id'] == actor_id:
        if order['status'] == 'PENDING':
            actions = [('Accept', 'accept'), ('Decline', 'decline')]
        elif order['status'] == 'ACCEPTED':
            actions = [('Mark fulfilled', 'complete'), ('Cancel order', 'askcancel')]
    elif order['status'] == 'PENDING':
        actions = [('Cancel order', 'cancel')]
    rows = []
    if actions:
        rows.append([InlineKeyboardButton(label, callback_data=f'ord:{action}:{oid}:{version}')
                     for label, action in actions])
    rows.append([InlineKeyboardButton('Refresh', callback_data=f'ord:view:{oid}:0')])
    return InlineKeyboardMarkup(rows)


class OrderHandlers:
    def __init__(self, handlers, enabled=False):
        self.base = handlers
        self.store = handlers.store
        self.run = handlers.run
        self.enabled = enabled

    async def failure(self, update, error):
        LOG.error('Order operation failed: category=%s', type(error).__name__)
        await update.effective_message.reply_text('The order service is unavailable. Please try again later.')

    def product_buttons(self, product, actor_id):
        if self.enabled and product['seller_telegram_user_id'] != actor_id and product['stock_quantity'] > 0:
            return InlineKeyboardMarkup([[InlineKeyboardButton('Order', callback_data=f'buy:{product["product_id"]}')]])
        return None

    async def start(self, update, context):
        if update.callback_query:
            await update.callback_query.answer()
        if update.effective_chat.type != 'private':
            await update.effective_message.reply_text('Please message me in a private chat.')
            return ConversationHandler.END
        await self.base.hide_phone_keyboard(update, context)
        self.base.reset_flow(context)
        if not self.enabled:
            await update.effective_message.reply_text('New orders are currently disabled. Existing orders remain in /orders and /sales.')
            return ConversationHandler.END
        raw = (update.callback_query.data.removeprefix('buy:') if update.callback_query
               else context.args[0] if context.args else None)
        if raw is None:
            await update.effective_message.reply_text('Send the catalog ID you want to order, for example 1 or #1.')
            return ORDER_ID
        try:
            pid = positive_int(raw)
        except OrderError as exc:
            await update.effective_message.reply_text(str(exc))
            return ORDER_ID
        return await self._begin_product(update, context, pid)

    async def product_id(self, update, context):
        try:
            pid = positive_int(update.effective_message.text)
        except OrderError as exc:
            await update.effective_message.reply_text(str(exc))
            return ORDER_ID
        return await self._begin_product(update, context, pid)

    async def _begin_product(self, update, context, pid):
        try:
            user = await self.run(self.store.get_user, update.effective_user.id)
            if not user:
                state = await self.base.start(update, context)
                context.user_data['order_after_signup'] = pid
                return state
            quote = await self.run(self.store.quote_order, update.effective_user.id, pid, 1)
        except OrderError as exc:
            await update.effective_message.reply_text(str(exc))
            return ConversationHandler.END
        except Exception as exc:
            await self.failure(update, exc)
            return ConversationHandler.END
        context.user_data['order_product_id'] = pid
        await update.effective_message.reply_text(
            f'How many of {quote["product_name"]} would you like? Send a positive whole number, or /cancel.')
        return ORDER_QUANTITY

    async def resume_signup(self, update, context):
        pid = context.user_data.pop('order_after_signup', None)
        if pid is not None and self.enabled:
            return await self._begin_product(update, context, pid)
        return ConversationHandler.END

    async def quantity(self, update, context):
        pid = context.user_data.get('order_product_id')
        last = context.user_data.get('last_input', {})
        if getattr(update, 'edited_message', None) is not None:
            if not pid or last.get('step') != 'order_quantity' or last.get('id') != update.effective_message.message_id:
                return None
        if not pid:
            await update.effective_message.reply_text('Start a new order with /order.')
            return ConversationHandler.END
        self.base.remember(context, update.effective_message, 'order_quantity')
        context.user_data.pop('order_draft', None)
        try:
            quote = await self.run(self.store.quote_order, update.effective_user.id, pid,
                                   positive_int(update.effective_message.text, 10**10 - 1))
        except OrderError as exc:
            await update.effective_message.reply_text(f'{exc} Try again or /cancel.')
            return ORDER_QUANTITY
        except Exception as exc:
            await self.failure(update, exc)
            return ORDER_QUANTITY
        key = uuid4().hex
        message = await update.effective_message.reply_text(summary_text(quote), reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('Confirm order', callback_data=f'confirm:{key}'),
            InlineKeyboardButton('Cancel', callback_data=f'discard:{key}'),
        ]]))
        context.user_data['order_draft'] = dict(key=key, quote=quote, message_id=message.message_id)
        return ORDER_CONFIRM

    async def confirm(self, update, context):
        query = update.callback_query
        await query.answer()
        if update.effective_chat.type != 'private':
            return None
        key = query.data.split(':', 1)[-1]
        try:
            old = await self.run(self.store.order_for_key, update.effective_user.id, key)
            if old:
                await self._show(update, old)
                return None
            draft = context.user_data.get('order_draft')
            if not draft or draft['key'] != key or query.message.message_id != draft['message_id']:
                await query.message.reply_text('This summary has expired. Use /order for a fresh review.')
                return None
            if query.data.startswith('discard:'):
                self.base.reset_flow(context)
                await query.message.reply_text('Order draft cancelled.')
                return ConversationHandler.END
            if not self.enabled:
                await query.message.reply_text('New orders are currently disabled.')
                return None
            order = await self.run(self.store.create_order, update.effective_user.id, draft['quote'], key)
        except OrderError as exc:
            await query.message.reply_text(str(exc))
            if exc.code in ('quote', 'stock'):
                context.user_data.pop('order_draft', None)
                await query.message.reply_text('Send the quantity again to review a new summary.')
                return ORDER_QUANTITY
            self.base.reset_flow(context)
            return ConversationHandler.END
        except Exception as exc:
            await self.failure(update, exc)
            return None
        self.base.reset_flow(context)
        await self._show(update, order)
        return ConversationHandler.END

    async def _show(self, update, order):
        await update.effective_message.reply_text(order_text(order, update.effective_user.id),
                                                  reply_markup=action_buttons(order, update.effective_user.id))

    async def purchases(self, update, context):
        await self.history(update, context, False)

    async def sales(self, update, context):
        await self.history(update, context, True)

    async def history(self, update, context, selling=False, offset=0):
        try:
            rows = await self.run(self.store.list_orders, update.effective_user.id, selling, offset, 11)
        except Exception as exc:
            await self.failure(update, exc)
            return
        title = 'Incoming orders' if selling else 'My orders'
        if not rows:
            await update.effective_message.reply_text(f'{title}: no orders on this page. Use /start to register if needed.')
            return
        page = rows[:10]
        lines = [title] + [f'#{o["order_id"]} · {o["product_name"]} × {o["quantity"]} · {STATUS_LABELS[o["status"]]}' for o in page]
        buttons = [[InlineKeyboardButton(f'Order #{o["order_id"]}', callback_data=f'ord:view:{o["order_id"]}:0')]
                   for o in page]
        navigation = []
        kind = 'sales' if selling else 'orders'
        if offset:
            navigation.append(InlineKeyboardButton('Previous', callback_data=f'hist:{kind}:{max(0, offset - 10)}'))
        if len(rows) > 10:
            navigation.append(InlineKeyboardButton('Next', callback_data=f'hist:{kind}:{offset + 10}'))
        if navigation:
            buttons.append(navigation)
        await update.effective_message.reply_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    async def durable_action(self, update, context):
        query = update.callback_query
        await query.answer()
        try:
            if update.effective_chat.type != 'private':
                await query.message.reply_text('Please message me in a private chat.')
                return
            if query.data.startswith('hist:'):
                _, kind, raw = query.data.split(':')
                if kind not in ('orders', 'sales') or not re.fullmatch(r'[0-9]{1,9}', raw):
                    raise OrderError('action', 'Invalid history page.')
                await self.history(update, context, kind == 'sales', int(raw))
                return
            _, action, raw, raw_version = query.data.split(':')
            oid, version = positive_int(raw), int(raw_version)
            order = await self.run(self.store.get_order, oid, update.effective_user.id)
            if not order:
                raise OrderError('missing', 'Order not found.')
            if action == 'askcancel':
                if order['seller_id'] != update.effective_user.id or order['status'] != 'ACCEPTED' or order['version'] != version:
                    await self._show(update, order)
                    return
                await query.message.reply_text(f'Cancel accepted order #{oid} and restore its stock?',
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton('Yes, cancel order', callback_data=f'ord:cancel:{oid}:{version}'),
                        InlineKeyboardButton('Keep order', callback_data=f'ord:view:{oid}:0'),
                    ]]))
                return
            if action != 'view':
                order = await self.run(self.store.change_order, oid, update.effective_user.id, action, version)
            await self._show(update, order)
        except (OrderError, ValueError) as exc:
            await query.message.reply_text(str(exc) if isinstance(exc, OrderError) else 'Invalid order action.')
        except Exception as exc:
            await self.failure(update, exc)
        finally:
            raise ApplicationHandlerStop
