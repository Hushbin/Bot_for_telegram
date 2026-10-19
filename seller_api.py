"""Loopback-only seller API, sharing the Telegram process and persistence adapters."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import logging
import secrets
import time
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from abuse_limits import LimitError
from orders import OrderError
from product_photos import MAX_PHOTO_BYTES, prepare_photo

COOKIE = '__Host-smebot'
SESSION_SECONDS = 8 * 60 * 60
LOGIN_SECONDS = 5 * 60


class SellerSessions:
    """Short-lived, one-use sign-in links; opaque sessions never stored in browsers' JS."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.links = {}
        self.sessions = {}

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def prune(self):
        now = self.clock()
        for collection in (self.links, self.sessions):
            for key, (_, expires) in list(collection.items()):
                if expires <= now:
                    collection.pop(key, None)

    def issue(self, uid):
        self.prune()
        # A new /panel command replaces this seller's previous unused link.
        self.links = {key: value for key, value in self.links.items() if value[0] != uid}
        if len(self.links) >= 10000:
            raise ValueError('Sign-in is busy. Try again shortly.')
        token = secrets.token_urlsafe(32)
        self.links[self.digest(token)] = (uid, self.clock() + LOGIN_SECONDS)
        return token

    def exchange(self, token):
        self.prune()
        item = self.links.pop(self.digest(token), None)
        if item is None:
            return None
        uid = item[0]
        # Keep at most five signed-in browsers for each seller.
        existing = [key for key, value in self.sessions.items() if value[0] == uid]
        for key in existing[:-4]:
            self.sessions.pop(key, None)
        if len(self.sessions) >= 10000:
            return None
        session = secrets.token_urlsafe(32)
        self.sessions[self.digest(session)] = (uid, self.clock() + SESSION_SECONDS)
        return session

    def user(self, token):
        self.prune()
        item = self.sessions.get(self.digest(token))
        return item[0] if item else None

    def revoke(self, token):
        self.sessions.pop(self.digest(token), None)


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def photo_revision(product):
    key = product.get('photo_object_key')
    return hashlib.sha256((key + ':' + str(product.get('photo_version_id') or '')).encode()).hexdigest() if key else 'none'


def panel_state(snapshot, orders_enabled):
    seller, user = snapshot['seller'], snapshot['user']
    uid = int(user['telegram_user_id'])
    products = [dict(id=int(p['product_id']), seller_id=uid, name=p['name'],
                     category=p['category'], description=p['description'] or '',
                     price_minor=int(p['price_minor']), stock_quantity=int(p['stock_quantity']),
                     currency=p['currency'], visibility='active',
                     photo_url=f"/api/products/{int(p['product_id'])}/photo" if p.get('photo_object_key') else None,
                     photo_revision=photo_revision(p)) for p in snapshot['products']]
    orders = []
    for o in snapshot['orders']:
        orders.append(dict(
            id=int(o['order_id']), seller_id=uid, status=o['status'].lower(),
            version=int(o['version']), currency=o['currency'],
            customer=dict(name=o['buyer_name'], phone=o['buyer_phone'],
                          phone_verified=bool(o['buyer_phone_verified'])),
            items=[dict(product_id=int(o['product_id']), name=o['product_name'],
                        quantity=int(o['quantity']), unit_price_minor=int(o['unit_price_minor']))],
            created_at=iso(o['created_at']), updated_at=iso(o['updated_at']),
            expires_at=iso(o['expires_at']), reserved=o['status'] == 'ACCEPTED'))
    return dict(seller=dict(id=uid, name=seller['business_name'], owner=user['full_name'],
                            email=seller['business_email'], location=seller['address'],
                            phone=seller['business_phone']), products=products, orders=orders,
                lowStock=5, orders_enabled=orders_enabled)


def create_api(store, sessions, origin, bot_username, orders_enabled=True, media=None):
    if urlsplit(origin).scheme != 'https' or urlsplit(origin).path not in ('', '/'):
        raise ValueError('SELLER_PANEL_URL must be an HTTPS origin without a path.')
    origin = origin.rstrip('/')

    async def endpoint(request: Request):
        path = request.url.path
        if path == '/api/health':
            return JSONResponse({'status': 'ok'})
        if path == '/api/config':
            return JSONResponse({'telegram_url': f'https://t.me/{bot_username}?start=seller_panel'})
        data = {}
        if request.method != 'GET':
            if (request.headers.get('origin') != origin
                    or request.headers.get('content-type', '').split(';')[0] != 'application/json'):
                return JSONResponse({'error': 'Invalid request origin or content type.'}, 403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({'error': 'Request is too large.'}, 413)
            try:
                import json
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return JSONResponse({'error': 'Invalid JSON object.'}, 400)
        if path == '/api/session' and request.method == 'POST':
            token = data.get('token')
            if not isinstance(token, str) or len(token) > 128:
                return JSONResponse({'error': 'Invalid sign-in link.'}, 401)
            session = sessions.exchange(token)
            if not session:
                return JSONResponse({'error': 'This sign-in link expired or was already used. Send /panel again.'}, 401)
            response = JSONResponse({'ok': True})
            response.set_cookie(COOKIE, session, max_age=SESSION_SECONDS, secure=True,
                                httponly=True, samesite='strict', path='/')
            return response
        cookie = request.cookies.get(COOKIE, '')
        uid = sessions.user(cookie)
        if uid is None:
            return JSONResponse({'error': 'Sign in through the bot to open your store.'}, 401)
        if path == '/api/logout':
            sessions.revoke(cookie)
            response = JSONResponse({'ok': True})
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
            return response
        try:
            # Every request rechecks the live seller, including after account erasure.
            snapshot = await asyncio.to_thread(store.seller_snapshot, uid)
            if path == '/api/state':
                return JSONResponse(panel_state(snapshot, orders_enabled))
            saved_product_id = None
            if path == '/api/products':
                saved_product_id = await asyncio.to_thread(store.save_seller_product, uid, data)
            elif path.startswith('/api/products/'):
                pid = int(request.path_params['pid'])
                if request.method == 'DELETE':
                    await asyncio.to_thread(store.remove_seller_product, uid, pid)
                else:
                    saved_product_id = await asyncio.to_thread(store.save_seller_product, uid, data, pid)
            elif path == '/api/profile':
                await asyncio.to_thread(store.save_seller_profile, uid, data)
            elif path.startswith('/api/orders/'):
                if not orders_enabled:
                    raise OrderError('permission', 'Ordering is currently disabled.')
                oid = int(request.path_params['oid'])
                order = next((o for o in snapshot['orders'] if o['order_id'] == oid), None)
                if order is None:
                    raise OrderError('missing', 'Order not found.')
                if type(data.get('version')) is not int or data['version'] != order['version']:
                    raise OrderError('conflict', 'This order changed. Refresh before trying again.')
                action = data.get('action')
                allowed = {'PENDING': ('accept', 'decline', 'cancel'),
                           'ACCEPTED': ('complete', 'cancel')}
                if action not in allowed.get(order['status'], ()):
                    raise OrderError('conflict', 'This action is no longer available.')
                await asyncio.to_thread(store.change_order, oid, uid, action, data['version'])
            snapshot = await asyncio.to_thread(store.seller_snapshot, uid)
            result = panel_state(snapshot, orders_enabled)
            if saved_product_id is not None:
                result['saved_product_id'] = int(saved_product_id)
            return JSONResponse(result)
        except LimitError as exc:
            headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after is not None else {}
            return JSONResponse({'error': str(exc), 'code': exc.code}, exc.status, headers=headers)
        except OrderError as exc:
            status = {'permission': 403, 'missing': 404, 'conflict': 409, 'stock': 409}.get(exc.code, 400)
            return JSONResponse({'error': str(exc)}, status)
        except Exception:
            logging.getLogger(__name__).exception('Seller API operation failed')
            return JSONResponse({'error': 'The store is temporarily unavailable. Please try again.'}, 503)

    photo_slots = asyncio.Semaphore(2)

    async def photo_endpoint(request):
        uid = sessions.user(request.cookies.get(COOKIE, ''))
        if uid is None:
            return JSONResponse({'error': 'Sign in through the bot to open your store.'}, 401)
        uploading = request.method == 'PUT'
        if uploading and (request.headers.get('origin') != origin or
                          request.headers.get('content-type', '').split(';')[0] not in ('image/jpeg', 'image/png')):
            return JSONResponse({'error': 'Use a JPEG or PNG upload from the seller panel.'}, 403)
        try:
            async with photo_slots:
                snapshot = await asyncio.to_thread(store.seller_snapshot, uid)
                pid = int(request.path_params['pid'])
                product = next((p for p in snapshot['products'] if p['product_id'] == pid), None)
                if product is None:
                    raise OrderError('missing', 'Product not found.')
                if media is None:
                    raise RuntimeError('Photo storage is not configured')
                if not uploading:
                    key = product.get('photo_object_key')
                    if not key:
                        raise OrderError('missing', 'This product has no photo yet.')
                    raw = await asyncio.to_thread(media.get_photo, key)
                    content_type = 'image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg'
                    return Response(raw, media_type=content_type)
                if request.headers.get('if-match') != photo_revision(product):
                    raise OrderError('conflict', 'The photo changed. Reopen the product before uploading again.')
                length = request.headers.get('content-length')
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        return JSONResponse({'error': 'Invalid content length.'}, 400)
                    if int(length) > MAX_PHOTO_BYTES:
                        return JSONResponse({'error': 'Choose a photo up to 5 MB.'}, 413)
                await asyncio.to_thread(store.admit_photo_upload, uid, pid,
                                        product.get('photo_object_key'), product.get('photo_version_id'))
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > MAX_PHOTO_BYTES:
                        return JSONResponse({'error': 'Choose a photo up to 5 MB.'}, 413)
                try:
                    prepared = await asyncio.to_thread(prepare_photo, raw)
                except ValueError as exc:
                    return JSONResponse({'error': str(exc)}, 400)

                def save_photo():
                    # Run upload + attach + compensation in one worker. A disconnected
                    # browser cannot cancel the database attach halfway through.
                    key, version = media.put_product_photo(uid, pid, prepared)
                    try:
                        previous = store.replace_seller_product_photo(
                            uid, pid, key, version, product.get('photo_object_key'))
                    except Exception:
                        try:
                            media.delete_photo(key, version)
                        except Exception:
                            logging.getLogger(__name__).exception('Could not remove unattached seller photo')
                        raise
                    if previous[0]:
                        try:
                            media.delete_photo(*previous)
                        except Exception:
                            logging.getLogger(__name__).exception('Could not remove replaced seller photo')

                await asyncio.to_thread(save_photo)
                snapshot = await asyncio.to_thread(store.seller_snapshot, uid)
                return JSONResponse(panel_state(snapshot, orders_enabled))
        except LimitError as exc:
            headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after is not None else {}
            return JSONResponse({'error': str(exc), 'code': exc.code}, exc.status, headers=headers)
        except OrderError as exc:
            return JSONResponse({'error': str(exc)}, {'permission': 403, 'missing': 404, 'conflict': 409}.get(exc.code, 400))
        except FileNotFoundError:
            return JSONResponse({'error': 'The photo is unavailable. Upload it again.'}, 404)
        except Exception:
            logging.getLogger(__name__).exception('Seller photo operation failed')
            return JSONResponse({'error': 'Photo storage is temporarily unavailable. Please try again.'}, 503)

    routes = [Route('/api/products/{pid:int}/photo', photo_endpoint, methods=['GET', 'PUT'])]
    routes += [Route(path, endpoint, methods=methods) for path, methods in (
        ('/api/health', ['GET']), ('/api/config', ['GET']), ('/api/session', ['POST']),
        ('/api/logout', ['POST']), ('/api/state', ['GET']), ('/api/products', ['POST']),
        ('/api/products/{pid:int}', ['PUT', 'DELETE']), ('/api/profile', ['PUT']),
        ('/api/orders/{oid:int}', ['POST']))]
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    return Starlette(routes=routes, middleware=[Middleware(BaseHTTPMiddleware, dispatch=no_cache)])


class SellerRuntime:
    def __init__(self, store, origin, port, orders_enabled, media=None):
        self.store, self.origin, self.port = store, origin.rstrip('/'), port
        self.orders_enabled = orders_enabled
        self.media = media
        self.sessions = SellerSessions()
        self.server = self.task = None

    async def panel(self, update, context):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import ConversationHandler
        uid = update.effective_user.id
        seller = await asyncio.to_thread(self.store.get_seller, uid)
        user = await asyncio.to_thread(self.store.get_user, uid)
        if not seller or not user:
            await update.effective_message.reply_text('Register with /start and create a /sellerprofile first, then send /panel.')
        else:
            token = self.sessions.issue(uid)
            await update.effective_message.reply_text(
                'Open your seller panel. This private sign-in link works once and expires in 5 minutes. '
                'Do not forward it. Signing in lasts up to 8 hours.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    'Open seller panel', url=f'{self.origin}/#login={token}')]]))
        return ConversationHandler.END

    async def start(self, application):
        import uvicorn
        class EmbeddedServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                # Telegram owns the process's signal handlers and shutdown order.
                yield

        api = create_api(self.store, self.sessions, self.origin, application.bot.username,
                         self.orders_enabled, media=self.media)
        self.server = EmbeddedServer(uvicorn.Config(api, host='127.0.0.1', port=self.port,
                                                   access_log=False, log_level='warning',
                                                   proxy_headers=False))
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.task.done():
                await self.task
                raise RuntimeError('Seller API exited during startup')
            if self.server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError('Seller API did not start')

    async def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.task:
            await self.task
            self.task = None
