"""Process-local inactivity deadlines for private conversations and corrections.

PTB still owns conversation states. Guards return END on the next participating
update after expiry; the sweeper only clears our draft fields, never PTB internals.
Updates are serialized by the application. Busy callbacks are admitted work and
are allowed to finish, including any database/media compensation.
"""
import asyncio
from dataclasses import dataclass
from functools import wraps
from time import monotonic

from telegram import ReplyKeyboardRemove
from telegram.ext import CommandHandler, ConversationHandler

IDLE_SECONDS = 30 * 60
SWEEP_SECONDS = 30
FLOW_FIELDS = (
    'last_input', 'draft', 'pending_product_id', 'pending_product_number',
    'signup_role', 'phone_keyboard_active', 'order_after_signup', 'order_product_id',
    'order_draft', 'product_draft', 'product_management', 'pending_photo_revision',
)
EXPIRED_TEXT = 'This step has expired after 30 minutes of inactivity. Send /menu to start again.'
RESTART_TEXT = 'There is no active step. Send /menu to choose an action and start again.'


def clear_flow(user_data):
    for key in FLOW_FIELDS:
        user_data.pop(key, None)


@dataclass
class Flow:
    data: dict
    deadline: float = 0
    active: bool = False
    busy: bool = False
    expired: bool = False
    notice: bool = False
    phone_keyboard: bool = False
    last_input_id: int | None = None


class ConversationLifecycle:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.flows = {}
        self.task = None
        self._notified_update = None

    def expire_due(self):
        """Clear abandoned input without sending messages or touching saved data."""
        now = self.clock()
        expired = 0
        for flow in self.flows.values():
            if flow.busy or flow.expired or now < flow.deadline:
                continue
            flow.phone_keyboard = bool(flow.data.get('phone_keyboard_active'))
            flow.last_input_id = flow.data.get('last_input', {}).get('id')
            clear_flow(flow.data)
            flow.expired = flow.notice = True
            expired += 1
        return expired

    @staticmethod
    def private(update):
        return (update.effective_user is not None and update.effective_chat is not None
                and update.effective_chat.type == 'private')

    @staticmethod
    def known_edit(update, flow):
        if update.edited_message is None:
            return True
        last_id = (flow.last_input_id if flow.expired else
                   flow.data.get('last_input', {}).get('id'))
        return last_id == update.effective_message.message_id

    async def notify(self, update, flow):
        marker = (update.effective_user.id, update.update_id)
        if marker == self._notified_update:
            return
        await update.effective_message.reply_text(
            EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove() if flow.phone_keyboard else None)
        flow.notice = False
        flow.phone_keyboard = False
        self._notified_update = marker

    async def observe(self, update, context):
        """Check deadlines even on read-only commands and durable buttons."""
        self._notified_update = None
        self.expire_due()
        if not self.private(update):
            return
        flow = self.flows.get(update.effective_user.id)
        if flow and flow.notice and self.known_edit(update, flow):
            await self.notify(update, flow)

    async def reject_expired(self, update, *, answer=True):
        """Also used *after* the durable order-key lookup in confirmation handling."""
        flow = self.flows.get(update.effective_user.id)
        if not flow or not flow.expired:
            return False
        if answer and update.callback_query:
            await update.callback_query.answer()
        await self.notify(update, flow)
        return True

    def wrap(self, callback, *, entry=False, fresh=False, confirmation=False,
             conversation=True):
        @wraps(callback)
        async def guarded(update, context):
            if not self.private(update):
                return await callback(update, context)
            # Guard independently of sweep cadence, including applications used
            # without runtime hooks (e.g. synthetic-update tests).
            self.expire_due()
            uid = update.effective_user.id
            flow = self.flows.get(uid)
            if update.edited_message is not None and (not flow or not self.known_edit(update, flow)):
                return None
            starts = fresh or (entry and update.callback_query and
                              update.callback_query.data.startswith(('menu:', 'buy:', 'pm:open:')))
            if flow and flow.expired and not starts and not confirmation:
                await self.reject_expired(update)
                self.flows.pop(uid, None)
                return ConversationHandler.END
            if flow is None:
                flow = Flow(context.user_data, deadline=self.clock() + IDLE_SECONDS)
                self.flows[uid] = flow
            was_expired = flow.expired
            flow.busy = True
            try:
                result = await callback(update, context)
            except BaseException:
                if not flow.active and not any(key in flow.data for key in FLOW_FIELDS):
                    self.flows.pop(uid, None)
                raise
            finally:
                flow.busy = False
            # Invalid/replayed entry buttons do not prolong a different draft.
            # Unknown edits were excluded above. State hints count as activity.
            renew = result is not None or (not entry and conversation and update.edited_message is None)
            if result is None and was_expired:
                result = ConversationHandler.END
                renew = True
            if renew:
                if conversation and result is not None:
                    flow.active = result != ConversationHandler.END
                flow.expired = flow.notice = False
                flow.last_input_id = None
                flow.deadline = self.clock() + IDLE_SECONDS
            if not flow.active and not any(key in flow.data for key in FLOW_FIELDS):
                self.flows.pop(uid, None)
            return result
        return guarded

    def guard_conversation(self, conversation, confirmation):
        """Wrap public callback lists; no dependency on PTB's state storage."""
        for handler in conversation.entry_points:
            handler.callback = self.wrap(handler.callback, entry=True,
                                         fresh=isinstance(handler, CommandHandler),
                                         confirmation=handler.callback == confirmation)
        for handlers in conversation.states.values():
            for handler in handlers:
                handler.callback = self.wrap(handler.callback)
        for handler in conversation.fallbacks:
            handler.callback = self.wrap(handler.callback, fresh=True)

    async def unhandled_input(self, update, context):
        flow = self.flows.get(update.effective_user.id)
        if flow and flow.expired:
            await self.notify(update, flow)
        else:
            await update.effective_message.reply_text(RESTART_TEXT, reply_markup=ReplyKeyboardRemove())

    async def _sweep(self):
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            self.expire_due()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._sweep(), name='conversation-expiry')

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
