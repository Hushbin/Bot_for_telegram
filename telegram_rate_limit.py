"""Bounded process-local Telegram admission, independent of durable write budgets."""

from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
from math import ceil
from time import monotonic

from telegram.ext import ApplicationHandlerStop, CommandHandler, filters


LOG = logging.getLogger(__name__)
MAX_USERS = 10_000
RESERVE_BURST = 3
RESERVE_REFILL_SECONDS = 10
FEEDBACK_SECONDS = 10
REPORT_SECONDS = 60
RECOVERY_COMMANDS = ('cancel', 'privacy', 'mydata', 'deleteme')


@dataclass(frozen=True)
class TelegramRateLimit:
    enabled: bool = False
    burst: int = 10
    refill_per_second: int = 1

    @classmethod
    def from_environment(cls, environ):
        prefix = 'TELEGRAM_RATE_LIMIT_'
        enabled = environ.get(prefix + 'ENABLED', 'false').lower()
        if enabled not in ('true', 'false'):
            raise ValueError(prefix + 'ENABLED must be true or false')
        values = {'enabled': enabled == 'true'}
        for name, default in (('burst', 10), ('refill_per_second', 1)):
            key = prefix + name.upper()
            raw = environ.get(key, str(default))
            if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 1000:
                raise ValueError(key + ' must be an integer between 1 and 1000')
            values[name] = int(raw)
        return cls(**values)


@dataclass
class UserBucket:
    tokens: float
    # Seconds of credit avoid accumulating fractional 0.1-token refills.
    reserve_seconds: float
    updated: float
    last_feedback: float = float('-inf')


@dataclass(frozen=True)
class Admission:
    allowed: bool
    retry_after: int = 0
    feedback: bool = False


class RequestLimiter:
    """Called synchronously by the serialized dispatcher; no tasks or locks needed."""

    def __init__(self, policy, clock=monotonic):
        self.policy = policy
        self.clock = clock
        self.users = OrderedDict()
        self.idle_seconds = max(60, policy.burst / policy.refill_per_second,
                                RESERVE_BURST * RESERVE_REFILL_SECONDS)
        self.reported_at = clock()
        self.counts = dict(accepted=0, rejected=0, reserve_used=0, capacity_rejected=0)

    def _record(self, now, *, allowed, reserve=False, capacity=False):
        self.counts['accepted' if allowed else 'rejected'] += 1
        self.counts['reserve_used'] += int(reserve)
        self.counts['capacity_rejected'] += int(capacity)
        if now - self.reported_at >= REPORT_SECONDS:
            try:
                LOG.info('TELEGRAM_RATE_LIMIT %s', json.dumps(dict(v=1, **self.counts),
                                                             separators=(',', ':')))
            except Exception:
                # Observability cannot change an admission decision.
                pass
            self.counts = dict.fromkeys(self.counts, 0)
            self.reported_at = now

    def admit(self, uid, *, recovery=False, feedback=True):
        if not self.policy.enabled:
            return Admission(True)
        now = self.clock()
        # Activity order makes cleanup proportional to expired entries, not all users.
        while self.users:
            oldest = next(iter(self.users.values()))
            if now - oldest.updated < self.idle_seconds:
                break
            self.users.popitem(last=False)
        bucket = self.users.get(uid)
        if bucket is None:
            if len(self.users) >= MAX_USERS:
                self._record(now, allowed=False, capacity=True)
                return Admission(False)
            bucket = UserBucket(self.policy.burst, RESERVE_BURST * RESERVE_REFILL_SECONDS, now)
            self.users[uid] = bucket
        elapsed = now - bucket.updated
        bucket.tokens = min(self.policy.burst,
                            bucket.tokens + elapsed * self.policy.refill_per_second)
        bucket.reserve_seconds = min(RESERVE_BURST * RESERVE_REFILL_SECONDS,
                                     bucket.reserve_seconds + elapsed)
        bucket.updated = now
        self.users.move_to_end(uid)
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            self._record(now, allowed=True)
            return Admission(True)
        if recovery and bucket.reserve_seconds >= RESERVE_REFILL_SECONDS:
            bucket.reserve_seconds -= RESERVE_REFILL_SECONDS
            self._record(now, allowed=True, reserve=True)
            return Admission(True)
        wait = (1 - bucket.tokens) / self.policy.refill_per_second
        if recovery:
            wait = min(wait, RESERVE_REFILL_SECONDS - bucket.reserve_seconds)
        notify = feedback and now - bucket.last_feedback >= FEEDBACK_SECONDS
        if notify:
            bucket.last_feedback = now
        self._record(now, allowed=False)
        return Admission(False, max(1, ceil(wait)), notify)


class TelegramRequestGate:
    def __init__(self, limiter, bot_commands):
        self.limiter = limiter
        # Reuse PTB's command/entity/mention matching without dispatching callbacks.
        self.commands = CommandHandler(bot_commands, self.check, filters=filters.UpdateType.MESSAGE)
        self.recovery = CommandHandler(RECOVERY_COMMANDS, self.check,
                                       filters=filters.UpdateType.MESSAGE)

    async def check(self, update, context):
        if not self.limiter.policy.enabled or update.effective_user is None:
            return
        query = update.callback_query
        chat = update.effective_chat
        private = chat is not None and chat.type == 'private'
        if not query:
            if update.message is None and update.edited_message is None:
                return
            if not private and not self.commands.check_update(update):
                return
        recovery = private and (
            bool(self.recovery.check_update(update)) if not query
            else query.data in ('erase:yes', 'erase:no'))
        decision = self.limiter.admit(update.effective_user.id, recovery=recovery,
                                      feedback=private or query is not None)
        if decision.allowed:
            return
        text = f'Too many requests. Try again in {decision.retry_after} seconds.'
        try:
            if query:
                await query.answer(text if decision.feedback else None)
            elif private and decision.feedback:
                await update.effective_message.reply_text(text)
        except Exception:
            # Do not enter the generic error handler, which sends another message.
            # Cancellation (BaseException) still propagates normally.
            pass
        raise ApplicationHandlerStop
