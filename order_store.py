"""Shared transactional order operations for Oracle and SQLite.

Oracle locks accounts before products and orders. Erasure locks its own account,
then products and orders. SQLite acquires the write lock before reading state.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import re
from uuid import uuid4

from orders import (CONSENT_VERSION, PENDING_HOURS, QUOTE_FIELDS, TIME_COLUMNS,
                    OrderError, as_utc, positive_int, quote_values)


class OrderDB:
    def __init__(self, connection, schema=None):
        self.connection = connection
        self.schema = schema
        self.cursor = connection.cursor()

    def table(self, name):
        return f'{self.schema}.{name}' if self.schema else name

    def execute(self, sql, **values):
        if self.schema:
            import oracledb
            self.cursor.setinputsizes(**{k: oracledb.DB_TYPE_TIMESTAMP_TZ
                                         for k, v in values.items() if isinstance(v, datetime)})
        else:
            values = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in values.items()}
        self.cursor.execute(sql, values)
        return self.cursor.rowcount

    def _row(self, row):
        if row is None:
            return None
        result = {column[0].lower(): value for column, value in zip(self.cursor.description, row)}
        for key in TIME_COLUMNS & result.keys():
            if result[key] is not None:
                result[key] = as_utc(result[key])
        return result

    def one(self, sql, **values):
        self.execute(sql, **values)
        return self._row(self.cursor.fetchone())

    def all(self, sql, **values):
        self.execute(sql, **values)
        return [self._row(row) for row in self.cursor.fetchall()]

    def lock(self, sql):
        return sql + ' FOR UPDATE' if self.schema else sql

    def now(self):
        if self.schema:
            return as_utc(self.one('SELECT SYSTIMESTAMP AS created_at FROM dual')['created_at'])
        return as_utc(self.one("SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') AS created_at")['created_at'])

    def insert(self, table, values, identity):
        sql = (f"INSERT INTO {self.table(table)} ({', '.join(values)}) VALUES "
               f"({', '.join(':' + key for key in values)})")
        if self.schema:
            import oracledb
            output = self.cursor.var(oracledb.DB_TYPE_NUMBER)
            self.execute(sql + f' RETURNING {identity} INTO :generated_id', **values, generated_id=output)
            return int(output.getvalue()[0])
        self.execute(sql, **values)
        return self.cursor.lastrowid

    def page(self, sql, limit, offset=0):
        if self.schema:
            return f'{sql} OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY'
        return f'{sql} LIMIT {limit} OFFSET {offset}'

    def accounts(self, *ids):
        if self.schema:
            for uid in sorted(set(ids)):
                self.one(self.lock(f"SELECT telegram_user_id FROM {self.table('users')} "
                                   'WHERE telegram_user_id = :account_id'), account_id=uid)


class OrderStoreMixin:
    @contextmanager
    def order_transaction(self, write=False):
        with self.connect() as connection:
            schema = getattr(self, 'schema', None)
            if write and not schema:
                connection.execute('BEGIN IMMEDIATE')
            db = OrderDB(connection, schema)
            try:
                yield db
                if write:
                    connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                db.cursor.close()

    def order_now(self):
        with self.order_transaction() as db:
            return self._order_now(db)

    def _order_now(self, db):
        clock = getattr(self, 'order_clock', None)
        return as_utc(clock()) if clock else db.now()

    @staticmethod
    def _live(db, table, uid):
        return db.one(f"SELECT * FROM {db.table(table)} "
                      'WHERE telegram_user_id = :account_id AND deleted_at IS NULL', account_id=uid)

    @staticmethod
    def _order(db, order_id, lock=False):
        sql = f"SELECT * FROM {db.table('orders')} WHERE order_id = :oid"
        return db.one(db.lock(sql) if lock else sql, oid=order_id)

    @staticmethod
    def _product(db, product_id, lock=False):
        sql = f"SELECT * FROM {db.table('products')} WHERE product_id = :pid"
        return db.one(db.lock(sql) if lock else sql, pid=product_id)

    def _quote(self, db, buyer_id, product_id, quantity, lock=False):
        product = self._product(db, product_id, lock)
        buyer = self._live(db, 'users', buyer_id)
        if not buyer:
            raise OrderError('registration', 'Register with /start before ordering.')
        if not product or product['deleted_at']:
            raise OrderError('unavailable', 'This product is no longer available.')
        seller = self._live(db, 'sellers', product['seller_telegram_user_id'])
        if not seller or not self._live(db, 'users', product['seller_telegram_user_id']):
            raise OrderError('unavailable', 'This seller is no longer available.')
        return quote_values(product, seller, buyer, quantity)

    def guard_stock(self, db, product_id, seller_id, quantity):
        product = self._product(db, product_id, lock=True)
        if not product or product['seller_telegram_user_id'] != seller_id or product['deleted_at']:
            return
        accepted = db.one(f"SELECT COALESCE(SUM(quantity), 0) AS quantity FROM {db.table('orders')} "
                          "WHERE product_id = :pid AND status = 'ACCEPTED'", pid=product_id)
        if not 0 <= quantity < 10**10 or quantity + int(accepted['quantity']) >= 10**10:
            raise OrderError('stock', 'Stock must leave room to return quantities in accepted orders.')

    def quote_order(self, buyer_id, product_id, quantity):
        with self.order_transaction() as db:
            return self._quote(db, buyer_id, positive_int(product_id), quantity)

    def order_for_key(self, buyer_id, confirmation_key):
        with self.order_transaction() as db:
            return db.one(f"SELECT * FROM {db.table('orders')} WHERE confirmation_key = :key "
                          'AND buyer_id = :buyer AND buyer_erased_at IS NULL',
                          key=confirmation_key, buyer=buyer_id) if self._live(db, 'users', buyer_id) else None

    def _notify(self, db, order, kind, recipients, now):
        for uid in set(recipients):
            if self._live(db, 'users', uid):
                db.insert('order_notifications', dict(
                    order_id=order['order_id'], order_version=order['version'], recipient_id=uid,
                    event_kind=kind, state='PENDING', attempt_count=0,
                    created_at=now, next_attempt_at=now), 'event_id')

    def create_order(self, buyer_id, quote, confirmation_key):
        if not re.fullmatch(r'[0-9a-f]{32}', confirmation_key):
            raise OrderError('draft', 'This confirmation has expired. Start a new order.')
        with self.order_transaction(True) as db:
            db.accounts(buyer_id, positive_int(quote['seller_id']))
            old = db.one(f"SELECT * FROM {db.table('orders')} WHERE confirmation_key = :key", key=confirmation_key)
            if old:
                if old['buyer_id'] != buyer_id or old['buyer_erased_at'] or not self._live(db, 'users', buyer_id):
                    raise OrderError('missing', 'Order not found.')
                return old
            current = self._quote(db, buyer_id, positive_int(quote['product_id']), quote['quantity'], lock=True)
            if any(quote.get(key) != current[key] for key in QUOTE_FIELDS):
                raise OrderError('quote', 'The listing or your contact details changed. Review a new summary.')
            now = self._order_now(db)
            values = dict(current, buyer_id=buyer_id, consent_version=CONSENT_VERSION,
                          confirmation_key=confirmation_key, status='PENDING', version=1,
                          created_at=now, updated_at=now, confirmed_at=now,
                          expires_at=now + timedelta(hours=PENDING_HOURS))
            oid = db.insert('orders', values, 'order_id')
            order = self._order(db, oid)
            self._notify(db, order, 'CREATED', [order['seller_id']], now)
            return order

    def get_order(self, order_id, actor_id):
        with self.order_transaction() as db:
            order = self._order(db, positive_int(order_id))
            if not self._visible(db, order, actor_id):
                return None
            failure = db.one(f"SELECT COUNT(*) AS count FROM {db.table('order_notifications')} "
                             "WHERE order_id = :oid AND state = 'FAILED'", oid=order_id)
            order['delivery_failed'] = bool(failure['count'])
            return order

    def _visible(self, db, order, actor_id):
        if not order or not self._live(db, 'users', actor_id):
            return False
        return ((order['buyer_id'] == actor_id and order['buyer_erased_at'] is None)
                or (order['seller_id'] == actor_id and order['seller_erased_at'] is None))

    def list_orders(self, actor_id, selling=False, offset=0, limit=10):
        offset = max(0, min(int(offset), 10**9))
        limit = max(1, min(int(limit), 20))
        party = 'seller' if selling else 'buyer'
        with self.order_transaction() as db:
            if not self._live(db, 'users', actor_id):
                return []
            return db.all(db.page(f"SELECT * FROM {db.table('orders')} WHERE {party}_id = :account_id "
                                 f'AND {party}_erased_at IS NULL ORDER BY order_id DESC', limit, offset),
                          account_id=actor_id)

    def _transition(self, db, order, status, now, actor=None, reason=None, recipients=None):
        db.execute(f"UPDATE {db.table('orders')} SET status = :status, version = version + 1, "
                   'updated_at = :now, decided_at = COALESCE(decided_at, :now), '
                   'completed_at = :completed, cancelled_by = :actor, cancellation_reason = :reason '
                   'WHERE order_id = :oid', status=status, now=now,
                   completed=now if status == 'COMPLETED' else None,
                   actor=actor if status == 'CANCELLED' else None, reason=reason, oid=order['order_id'])
        order = self._order(db, order['order_id'])
        self._notify(db, order, status, recipients if recipients is not None else
                     [order['buyer_id'], order['seller_id']], now)
        return order

    @staticmethod
    def _restock(db, order):
        changed = db.execute(f"UPDATE {db.table('products')} SET stock_quantity = stock_quantity + :qty "
                             'WHERE product_id = :pid AND deleted_at IS NULL '
                             'AND stock_quantity + :qty < 10000000000 '
                             f"AND EXISTS (SELECT 1 FROM {db.table('users')} WHERE telegram_user_id = :sid "
                             'AND deleted_at IS NULL)', qty=int(order['quantity']),
                             pid=order['product_id'], sid=order['seller_id'])
        if changed != 1:
            product = OrderStoreMixin._product(db, order['product_id'])
            if product and not product['deleted_at'] and OrderStoreMixin._live(db, 'users', order['seller_id']):
                raise OrderError('stock', 'Stock would exceed the allowed limit; adjust stock before cancelling.')

    def change_order(self, order_id, actor_id, action, version):
        with self.order_transaction(True) as db:
            order = self._order(db, positive_int(order_id))
            if not order:
                raise OrderError('missing', 'Order not found.')
            db.accounts(order['buyer_id'], order['seller_id'])
            self._product(db, order['product_id'], lock=True)
            order = self._order(db, order_id, lock=True)
            if not self._visible(db, order, actor_id):
                raise OrderError('missing', 'Order not found.')
            if action not in ('accept', 'decline', 'cancel', 'complete'):
                raise OrderError('action', 'Unknown order action.')
            is_seller = actor_id == order['seller_id']
            if not is_seller and action != 'cancel':
                raise OrderError('permission', "Only this order's seller can perform that action.")
            now = self._order_now(db)
            if order['status'] == 'PENDING' and now >= order['expires_at']:
                return self._transition(db, order, 'EXPIRED', now)
            if order['version'] != version:
                return order
            if action == 'accept' and order['status'] == 'PENDING':
                if not (self._live(db, 'users', order['buyer_id']) and self._live(db, 'sellers', order['seller_id'])):
                    raise OrderError('unavailable', 'An account is no longer available.')
                count = db.execute(f"UPDATE {db.table('products')} SET stock_quantity = stock_quantity - :qty "
                                   'WHERE product_id = :pid AND seller_telegram_user_id = :sid '
                                   'AND deleted_at IS NULL AND stock_quantity >= :qty',
                                   qty=int(order['quantity']), pid=order['product_id'], sid=order['seller_id'])
                if count != 1:
                    raise OrderError('stock', 'There is not enough stock to accept this order.')
                status = 'ACCEPTED'
            elif action == 'decline' and order['status'] == 'PENDING':
                status = 'DECLINED'
            elif action == 'complete' and order['status'] == 'ACCEPTED':
                status = 'COMPLETED'
            elif action == 'cancel' and (order['status'] == 'PENDING' or is_seller and order['status'] == 'ACCEPTED'):
                if order['status'] == 'ACCEPTED':
                    self._restock(db, order)
                status = 'CANCELLED'
            else:
                return order
            recipient = order['buyer_id'] if is_seller else order['seller_id']
            return self._transition(db, order, status, now, actor_id,
                                    'REQUESTED' if status == 'CANCELLED' else None, [recipient])

    def expire_orders(self, limit=20):
        with self.order_transaction() as db:
            now = self._order_now(db)
            candidates = db.all(db.page(f"SELECT order_id, buyer_id FROM {db.table('orders')} "
                                       "WHERE status = 'PENDING' AND expires_at <= :now ORDER BY order_id", limit), now=now)
        for item in candidates:
            with self.order_transaction(True) as db:
                order = self._order(db, item['order_id'])
                db.accounts(order['buyer_id'], order['seller_id'])
                self._product(db, order['product_id'], lock=True)
                order = self._order(db, order['order_id'], lock=True)
                now = self._order_now(db)
                if order['status'] == 'PENDING' and now >= order['expires_at']:
                    self._transition(db, order, 'EXPIRED', now)
        return len(candidates)

    def claim_notification(self):
        with self.order_transaction(True) as db:
            now = self._order_now(db)
            candidates = db.all(db.page(f"SELECT event_id FROM {db.table('order_notifications')} "
                                       "WHERE (state = 'PENDING' AND next_attempt_at <= :now) "
                                       "OR (state = 'SENDING' AND lease_until <= :now) "
                                       'ORDER BY next_attempt_at, event_id', 20), now=now)
            for candidate in candidates:
                event = db.one(db.lock(f"SELECT * FROM {db.table('order_notifications')} WHERE event_id = :eid"),
                               eid=candidate['event_id'])
                if not ((event['state'] == 'PENDING' and event['next_attempt_at'] <= now)
                        or (event['state'] == 'SENDING' and event['lease_until'] <= now)):
                    continue
                token = uuid4().hex
                db.execute(f"UPDATE {db.table('order_notifications')} SET state = 'SENDING', "
                           'lease_token = :token, lease_until = :until, attempt_count = attempt_count + 1 '
                           'WHERE event_id = :eid', token=token, until=now + timedelta(seconds=90), eid=event['event_id'])
                return db.one(f"SELECT * FROM {db.table('order_notifications')} WHERE event_id = :eid", eid=event['event_id'])
        return None

    def notification_order(self, event):
        with self.order_transaction() as db:
            job = db.one(f"SELECT * FROM {db.table('order_notifications')} WHERE event_id = :eid", eid=event['event_id'])
            order = self._order(db, event['order_id'])
            if (not job or job['state'] != 'SENDING' or job['lease_token'] != event['lease_token']
                    or job['lease_until'] <= self._order_now(db)
                    or not self._visible(db, order, event['recipient_id']) or order['version'] != event['order_version']):
                return None
            return order

    def finish_notification(self, event, state, error=None, retry_seconds=0, message_id=None):
        if state not in ('SENT', 'FAILED', 'SUPPRESSED', 'PENDING'):
            raise ValueError('Invalid notification state')
        with self.order_transaction(True) as db:
            now = self._order_now(db)
            return db.execute(f"UPDATE {db.table('order_notifications')} SET state = :state, "
                              'next_attempt_at = :next_at, lease_token = NULL, lease_until = NULL, '
                              'last_error = :error, message_id = :mid, delivered_at = :delivered '
                              "WHERE event_id = :eid AND state = 'SENDING' AND lease_token = :token",
                              state=state, next_at=now + timedelta(seconds=retry_seconds),
                              error=error[:40] if error else None, mid=message_id,
                              delivered=now if state == 'SENT' else None,
                              eid=event['event_id'], token=event['lease_token']) == 1

    def erase_orders(self, db, user_id):
        now = self._order_now(db)
        candidates = db.all(f"SELECT * FROM {db.table('orders')} WHERE buyer_id = :account_id OR seller_id = :account_id "
                            'ORDER BY product_id, order_id', account_id=user_id)
        for candidate in candidates:
            self._product(db, candidate['product_id'], lock=True)
            order = self._order(db, candidate['order_id'], lock=True)
            buyer = user_id == order['buyer_id']
            own_column = 'buyer_erased_at' if buyer else 'seller_erased_at'
            if order[own_column] is not None:
                continue
            assignments = ('buyer_name = :erased, buyer_phone = :erased, buyer_phone_verified = 0'
                           if buyer else 'seller_name = :erased, product_name = :erased')
            db.execute(f"UPDATE {db.table('orders')} SET {assignments}, {own_column} = :now, "
                       'version = version + 1, updated_at = :now WHERE order_id = :oid',
                       erased='[deleted]', now=now, oid=order['order_id'])
            db.execute(f"UPDATE {db.table('order_notifications')} SET state = 'SUPPRESSED', "
                       'lease_token = NULL, lease_until = NULL '
                       "WHERE order_id = :oid AND state IN ('PENDING', 'SENDING')", oid=order['order_id'])
            if order['status'] in ('PENDING', 'ACCEPTED'):
                if order['status'] == 'ACCEPTED' and buyer:
                    self._restock(db, order)
                order = self._order(db, order['order_id'])
                recipient = order['seller_id'] if buyer else order['buyer_id']
                self._transition(db, order, 'CANCELLED', now, user_id, 'ACCOUNT_ERASED', [recipient])
