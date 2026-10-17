"""Order values and validation, independent of Telegram and database adapters."""

from datetime import datetime, timezone
from decimal import Decimal
import re

CONSENT_VERSION = 'orders-v1'
PENDING_HOURS = 24
STATUSES = ('PENDING', 'ACCEPTED', 'DECLINED', 'CANCELLED', 'EXPIRED', 'COMPLETED')
STATUS_LABELS = dict(PENDING='Awaiting seller', ACCEPTED='Accepted', DECLINED='Declined',
                     CANCELLED='Cancelled', EXPIRED='Expired', COMPLETED='Completed')
QUOTE_FIELDS = ('product_id', 'seller_id', 'product_name', 'seller_name', 'unit_price_minor',
                'currency', 'quantity', 'buyer_name', 'buyer_phone', 'buyer_phone_verified')
TIME_COLUMNS = {'created_at', 'updated_at', 'expires_at', 'decided_at', 'completed_at',
                'buyer_erased_at', 'seller_erased_at', 'confirmed_at', 'next_attempt_at',
                'lease_until', 'delivered_at', 'deleted_at'}


class OrderError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def positive_int(value, maximum=2**63 - 1):
    raw = str(value).strip().removeprefix('#')
    if not re.fullmatch(r'[0-9]{1,19}', raw) or not 0 < int(raw) <= maximum:
        raise OrderError('quantity', 'Enter a positive whole number within the allowed range.')
    return int(raw)


def as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def money(minor, currency):
    return f'{currency} {Decimal(int(minor)) / 100:.2f}'


def quote_values(product, seller, buyer, quantity):
    quantity = positive_int(quantity, 10**10 - 1)
    if product['seller_telegram_user_id'] == buyer['telegram_user_id']:
        raise OrderError('self', 'You cannot order your own product.')
    if quantity > product['stock_quantity']:
        raise OrderError('stock', 'There is not enough stock for that quantity.')
    return dict(product_id=int(product['product_id']), seller_id=int(product['seller_telegram_user_id']),
                product_name=product['name'], seller_name=seller['business_name'],
                unit_price_minor=int(product['price_minor']), currency=product['currency'], quantity=quantity,
                buyer_name=buyer['full_name'], buyer_phone=buyer['phone'],
                buyer_phone_verified=int(buyer['phone_verified']))


def summary_text(quote):
    source = 'verified by Telegram' if quote['buyer_phone_verified'] else 'unverified, typed by you'
    return (
        f'{quote["product_name"]} × {quote["quantity"]}\nSeller: {quote["seller_name"]}\n'
        f'Unit price: {money(quote["unit_price_minor"], quote["currency"])}\n'
        f'Total: {money(int(quote["unit_price_minor"]) * quote["quantity"], quote["currency"])}\n\n'
        f'Confirming shares your name ({quote["buyer_name"]}) and phone '
        f'({quote["buyer_phone"]}, {source}) with this seller.\n'
        'Availability awaits seller acceptance; stock is not reserved. '
        'Payment and delivery are arranged with the seller.'
    )


def order_text(order, viewer_id):
    lines = [f'Order #{order["order_id"]} · {STATUS_LABELS[order["status"]]}',
             f'{order["product_name"]} × {order["quantity"]}', f'Seller: {order["seller_name"]}',
             f'Unit price: {money(order["unit_price_minor"], order["currency"])}',
             f'Total: {money(int(order["unit_price_minor"]) * int(order["quantity"]), order["currency"])}']
    source = 'verified by Telegram' if order['buyer_phone_verified'] else 'unverified'
    if viewer_id == order['seller_id']:
        lines.append(f'Buyer: {order["buyer_name"]}\nPhone: {order["buyer_phone"]} ({source})')
    else:
        lines.append(f'Your confirmed contact: {order["buyer_name"]}, {order["buyer_phone"]} ({source})')
        lines.append(f'Contact-sharing consent: {order["consent_version"]}, '
                     + as_utc(order['confirmed_at']).strftime('%Y-%m-%d %H:%M UTC'))
        if order['status'] == 'ACCEPTED':
            lines.append('The seller will contact you to arrange payment and fulfillment. Ask the seller to cancel if needed.')
    if order['status'] == 'PENDING':
        lines.append('Stock is not reserved. Expires: ' + as_utc(order['expires_at']).strftime('%Y-%m-%d %H:%M UTC'))
    if order.get('delivery_failed'):
        lines.append('A notification could not be delivered. This history shows the current status.')
    return '\n'.join(lines)
