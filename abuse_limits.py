"""Per-account admission policy. Counters contain no request history or contact data."""
from dataclasses import dataclass, fields
from datetime import timedelta
from math import ceil

from orders import OrderError


@dataclass(frozen=True)
class AbuseLimits:
    enabled: bool = False
    active_listings: int = 50
    listings_daily: int = 10
    listing_cooldown: int = 10
    orders_daily: int = 20
    order_cooldown: int = 30
    pending_orders: int = 10
    pending_per_seller: int = 3
    photos_daily: int = 20
    photo_cooldown: int = 10

    @classmethod
    def from_environment(cls, environ):
        enabled = environ.get('ABUSE_LIMITS_ENABLED', 'false').lower()
        if enabled not in ('true', 'false'):
            raise ValueError('ABUSE_LIMITS_ENABLED must be true or false')
        values = {'enabled': enabled == 'true'}
        for field in fields(cls):
            if field.name == 'enabled':
                continue
            key = 'ABUSE_' + field.name.upper()
            raw = environ.get(key, str(field.default))
            if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 1000000:
                raise ValueError(f'{key} must be an integer between 1 and 1000000')
            values[field.name] = int(raw)
        return cls(**values)


class LimitError(OrderError):
    def __init__(self, code, message, retry_after=None):
        super().__init__(code, message)
        self.retry_after = retry_after
        self.status = 429 if retry_after is not None else 409


def midnight(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def wait_error(code, label, until, now):
    seconds = max(1, ceil((until - now).total_seconds()))
    return LimitError(code, f'{label} Try again in {seconds} seconds '
                      f'({until:%Y-%m-%d %H:%M:%S} UTC).', seconds)


def summary_text(row):
    values = []
    for key, label in (('listing_count', 'New listings'), ('order_count', 'New orders'),
                       ('photo_attempt_count', 'Photo attempts')):
        values.append(f'{label}: {row[key]}')
    for key, label in (('last_listing_at', 'Last listing'), ('last_order_at', 'Last order'),
                       ('last_photo_at', 'Last photo attempt'), ('blocked_until', 'Recovery pause until')):
        if row[key] is not None:
            values.append(f'{label}: {row[key]:%Y-%m-%d %H:%M:%S} UTC')
    return 'Current UTC-day abuse-prevention data (resets at midnight UTC):\n' + '\n'.join(values)


class AbuseStoreMixin:
    abuse_limits = AbuseLimits()

    def _admit(self, db, uid, kind, seller_id=None):
        """Caller holds the account lock; successful work commits this same transaction."""
        row = db.one(db.lock(f"SELECT * FROM {db.table('abuse_limits')} "
                             'WHERE telegram_user_id = :account_id'), account_id=uid)
        now = self._order_now(db)
        start = midnight(now)
        end = start + timedelta(days=1)
        if row and row['blocked_until'] and now < row['blocked_until']:
            raise wait_error('restore_block', 'New activity is paused after database recovery.',
                             row['blocked_until'], now)
        if row is None:
            db.execute(f"INSERT INTO {db.table('abuse_limits')} (telegram_user_id, window_start) "
                       'VALUES (:account_id, :window_floor)', account_id=uid, window_floor=start)
        elif row['window_start'] != start:
            db.execute(f"UPDATE {db.table('abuse_limits')} SET window_start = :window_floor, "
                       'listing_count = 0, order_count = 0, photo_attempt_count = 0, '
                       'last_listing_at = NULL, last_order_at = NULL, last_photo_at = NULL, '
                       'blocked_until = NULL WHERE telegram_user_id = :account_id', account_id=uid, window_floor=start)
            row = None
        policy = self.abuse_limits
        counter, last, daily, cooldown = {
            'listing': ('listing_count', 'last_listing_at', policy.listings_daily, policy.listing_cooldown),
            'order': ('order_count', 'last_order_at', policy.orders_daily, policy.order_cooldown),
            'photo': ('photo_attempt_count', 'last_photo_at', policy.photos_daily, policy.photo_cooldown),
        }[kind]
        if policy.enabled:
            if row and row[counter] >= daily:
                raise wait_error(kind + '_daily', f'Daily {kind} limit reached.', end, now)
            if row and row[last] and now < row[last] + timedelta(seconds=cooldown):
                raise wait_error(kind + '_cooldown', f'Please wait before another {kind}.',
                                 row[last] + timedelta(seconds=cooldown), now)
            if kind == 'listing':
                count = db.one(f"SELECT COUNT(*) AS count FROM {db.table('products')} "
                               'WHERE seller_telegram_user_id = :account_id AND deleted_at IS NULL', account_id=uid)['count']
                if count >= policy.active_listings:
                    raise LimitError('active_listings', 'Active listing limit reached. Remove a listing before adding another.')
            elif kind == 'order':
                counts = db.one(f"SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN seller_id = :sid "
                                f"THEN 1 ELSE 0 END), 0) AS seller_count FROM {db.table('orders')} "
                                "WHERE buyer_id = :account_id AND status = 'PENDING' AND expires_at > :now",
                                account_id=uid, sid=seller_id, now=now)
                if counts['total'] >= policy.pending_orders or counts['seller_count'] >= policy.pending_per_seller:
                    raise LimitError('pending_orders', 'Pending order limit reached. Cancel a pending order or wait for a seller response or expiry.')
        db.execute(f"UPDATE {db.table('abuse_limits')} SET {counter} = {counter} + 1, "
                   f'{last} = :now WHERE telegram_user_id = :account_id', now=now, account_id=uid)

    def admit_photo_upload(self, uid, pid, expected_key=None, expected_version=None):
        """Commit the attempt before any external I/O; recheck ownership and revision."""
        with self.order_transaction(True) as db:
            db.accounts(uid)
            if not self._live(db, 'users', uid) or not self._live(db, 'sellers', uid):
                raise OrderError('permission', 'Register as a seller in the bot first.')
            product = self._product(db, pid, lock=True)
            if not product or product['seller_telegram_user_id'] != uid or product['deleted_at']:
                raise OrderError('missing', 'Product not found.')
            if (product['photo_object_key'], product['photo_version_id']) != (expected_key, expected_version):
                raise OrderError('conflict', 'The photo changed. Reopen the product before uploading again.')
            self._admit(db, uid, 'photo')

    def abuse_summary(self, uid):
        with self.order_transaction() as db:
            row = db.one(f"SELECT * FROM {db.table('abuse_limits')} WHERE telegram_user_id = :account_id", account_id=uid)
            return row if row and row['window_start'] == midnight(self._order_now(db)) else None

    def clear_expired_abuse_limits(self):
        # Ledger-only update: never acquire account/product locks after these locks.
        with self.order_transaction(True) as db:
            start = midnight(self._order_now(db))
            return db.execute(f"UPDATE {db.table('abuse_limits')} SET window_start = :window_floor, "
                              'listing_count = 0, order_count = 0, photo_attempt_count = 0, '
                              'last_listing_at = NULL, last_order_at = NULL, last_photo_at = NULL, '
                              'blocked_until = NULL WHERE window_start < :window_floor '
                              'AND (blocked_until IS NULL OR blocked_until <= :window_floor)', window_floor=start)
