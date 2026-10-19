"""Private Telegram field editor, photo replacement and removal confirmations."""
import re
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from catalog import CATEGORIES, MAX_PRODUCT_ID, category_label
from orders import OrderError, money
from product_media import remove_product
from seller_store import EDIT_FIELDS, product_snapshot

PM_ID, PM_MENU, PM_INPUT, PM_REVIEW, PM_REMOVE, PM_PHOTO = range(40, 46)
LABELS = dict(name='Name', category='Category', price_minor='Price',
              stock_quantity='Stock', description='Description')


def management_button(product):
    return InlineKeyboardButton(f'Manage #{product["seller_number"]}',
                                callback_data=f'pm:open:{product["product_id"]}')


def display_value(field, value):
    if field == 'category':
        return category_label(value)
    if field == 'price_minor':
        return money(value, 'USD')
    return str(value) if value is not None else '(none)'


def clean_value(field, text):
    # The existing creation validators are also the user-facing edit contract.
    from handlers import (clean_name, clean_product_category, clean_product_price,
                          clean_product_stock, clean_product_description)
    return dict(name=clean_name, category=clean_product_category, price_minor=clean_product_price,
                stock_quantity=clean_product_stock, description=clean_product_description)[field](text)


def management_text(product):
    from handlers import product_text
    return (product_text(dict(product, description=None), seller_view=True, detail=True) +
            '\nDescription: ' + (product['description'] or '(none)') +
            '\nPhoto: ' + ('attached' if product['photo_object_key'] else '(none)'))


class ProductManagementMixin:
    async def _management_product(self, uid, pid):
        if not await self.run(self.store.get_seller, uid):
            raise OrderError('permission', 'Register a seller profile with /sellerprofile first.')
        product = await self.run(self.store.get_product, pid)
        if not product or product['seller_telegram_user_id'] != uid:
            raise OrderError('missing', 'That product is not yours or has been removed.')
        return product

    async def _management_prompt(self, update, context, draft, text, actions):
        draft = dict(draft, token=uuid4().hex, message_id=None)
        context.user_data['product_management'] = draft
        rows = [[InlineKeyboardButton(label, callback_data=f'pm:{draft["token"]}:{action}')
                 for label, action in row] for row in actions]
        message = await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
        draft['message_id'] = message.message_id

    async def management_home(self, update, context, pid):
        product = await self._management_product(update.effective_user.id, pid)
        for key in ('last_input', 'pending_product_id', 'pending_product_number', 'pending_photo_revision'):
            context.user_data.pop(key, None)
        draft = dict(uid=update.effective_user.id, pid=pid, phase='menu')
        actions = [[(LABELS['name'], 'field_name'), (LABELS['category'], 'field_category')],
                   [(LABELS['price_minor'], 'field_price_minor'), (LABELS['stock_quantity'], 'field_stock_quantity')],
                   [(LABELS['description'], 'field_description')],
                   [('Replace photo' if product['photo_object_key'] else 'Add photo', 'photo')],
                   [('Remove product', 'remove'), ('Cancel', 'cancel')]]
        await self._management_prompt(update, context, draft,
            management_text(product) + '\n\nChoose what to change.', actions)
        return PM_MENU

    async def manage_product(self, update, context):
        await self.hide_phone_keyboard(update, context)
        self.reset_flow(context)
        if not context.args:
            await update.effective_message.reply_text(
                'Send your product number, or G followed by its catalog ID. You can also use Manage in /myproducts.')
            return PM_ID
        return await self._management_reference(update, context, context.args[0])

    async def management_id(self, update, context):
        return await self._management_reference(update, context, update.effective_message.text)

    async def _management_reference(self, update, context, reference):
        try:
            product = await self.owned_product(update.effective_user.id, reference)
            if not product:
                raise OrderError('missing', 'That product is not yours or has been removed.')
            return await self.management_home(update, context, product['product_id'])
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return PM_ID
        except Exception:
            await self.failure(update)
            return PM_ID

    async def _management_review(self, update, context, draft, value):
        draft = dict(draft, phase='review', value=value)
        field = draft['field']
        await self._management_prompt(update, context, draft,
            f'{LABELS[field]}\nCurrent: {display_value(field, draft["original"])}\n'
            f'New: {display_value(field, value)}\n\nSave this change?',
            [[('Save', 'save'), ('Back', 'back'), ('Cancel', 'cancel')]])
        return PM_REVIEW

    async def management_input(self, update, context):
        draft = context.user_data.get('product_management')
        edited = getattr(update, 'edited_message', None) is not None
        last = context.user_data.get('last_input', {})
        if not draft or draft['phase'] not in ('input', 'review') or 'field' not in draft:
            return None
        if edited and (last.get('step') != 'product_edit' or last.get('id') != update.effective_message.message_id):
            return None
        field = draft['field']
        if field == 'category':
            return None
        self.remember(context, update.effective_message, 'product_edit')
        try:
            value = clean_value(field, update.effective_message.text)
        except ValueError as exc:
            # Invalid corrections revoke the old Save confirmation too.
            await self._management_prompt(update, context, dict(draft, phase='input'),
                f'{exc} Send a replacement {LABELS[field].lower()}, or /cancel.',
                [[('Back', 'back'), ('Cancel', 'cancel')]])
            return PM_INPUT
        return await self._management_review(update, context, draft, value)

    async def management_photo_skip(self, update, context):
        draft = context.user_data.get('product_management')
        if not draft or draft['phase'] != 'photo':
            return None
        try:
            return await self.management_home(update, context, draft['pid'])
        except OrderError as exc:
            self.reset_flow(context)
            await update.effective_message.reply_text(str(exc))
            return ConversationHandler.END

    async def management_action(self, update, context):
        query = update.callback_query
        await query.answer()
        if update.effective_chat.type != 'private':
            await query.message.reply_text('Please message me in a private chat.')
            return None
        try:
            data = query.data
            if len(data.encode()) > 64:
                raise ValueError
            if data.startswith('pm:open:'):
                raw = data.removeprefix('pm:open:')
                if not re.fullmatch(r'[0-9]{1,19}', raw) or not 0 < int(raw) <= MAX_PRODUCT_ID:
                    raise ValueError
                pid = int(raw)
                # A forged open button must not discard another active draft.
                await self._management_product(update.effective_user.id, pid)
                await self.hide_phone_keyboard(update, context)
                self.reset_flow(context)
                return await self.management_home(update, context, pid)
            _, token, action = data.split(':')
            draft = context.user_data.get('product_management')
            if (not draft or not re.fullmatch(r'[0-9a-f]{32}', token) or draft['token'] != token
                    or draft['uid'] != update.effective_user.id or draft['message_id'] != query.message.message_id):
                raise ValueError
        except OrderError as exc:
            await query.message.reply_text(str(exc))
            return None
        except (ValueError, TypeError):
            await query.message.reply_text('This management button has expired. Use /manageproduct or /myproducts.')
            return None
        except Exception:
            await self.failure(update)
            return None
        try:
            pid = draft['pid']
            if action == 'cancel':
                return await self.cancel(update, context)
            if action == 'back':
                return await self.management_home(update, context, pid)
            if draft['phase'] == 'menu' and action.startswith('field_'):
                field = action.removeprefix('field_')
                if field not in EDIT_FIELDS:
                    raise ValueError
                product = await self._management_product(update.effective_user.id, pid)
                draft = dict(draft, field=field, original=product[field], phase='input')
                context.user_data.pop('last_input', None)
                actions = [[('Back', 'back'), ('Cancel', 'cancel')]]
                if field == 'category':
                    actions = [[(label, 'cat_' + key)] for key, label in CATEGORIES.items()] + actions
                elif field == 'description':
                    actions.insert(0, [('Clear description', 'clear')])
                prompt = f'Current {LABELS[field].lower()}: {display_value(field, product[field])}\n'
                prompt += ('Choose a category below.' if field == 'category' else
                           f'Send a new {LABELS[field].lower()}' +
                           (' in USD.' if field == 'price_minor' else '.'))
                await self._management_prompt(update, context, draft,
                                              prompt, actions)
                return PM_INPUT
            if draft['phase'] == 'input' and draft.get('field') == 'category' and action.startswith('cat_'):
                key = action.removeprefix('cat_')
                if key not in CATEGORIES:
                    raise ValueError
                return await self._management_review(update, context, draft, key)
            if draft['phase'] == 'input' and draft.get('field') == 'description' and action == 'clear':
                context.user_data.pop('last_input', None)
                return await self._management_review(update, context, draft, None)
            if draft['phase'] == 'review' and action == 'save':
                await self.run(self.store.edit_seller_product_field, update.effective_user.id,
                               pid, draft['field'], draft['value'], draft['original'])
                self.reset_flow(context)  # Consume success before Telegram I/O.
                await query.message.reply_text(f'{LABELS[draft["field"]]} updated.')
                return await self.management_home(update, context, pid)
            if draft['phase'] == 'menu' and action == 'remove':
                product = await self._management_product(update.effective_user.id, pid)
                await self._management_prompt(update, context,
                    dict(draft, phase='remove', snapshot=product_snapshot(product)),
                    management_text(product) + '\n\nRemove this product?\n'
                    'It will disappear from the catalog and its photo will be deleted. '
                    'Order history remains. Resolve pending and accepted orders in /sales first. '
                    'There is no undo in the bot.',
                    [[('Confirm removal', 'delete'), ('Back', 'back'), ('Cancel', 'cancel')]])
                return PM_REMOVE
            if draft['phase'] == 'remove' and action == 'delete':
                await self.run(remove_product, self.store, self.media, update.effective_user.id,
                               pid, draft['snapshot'])
                self.reset_flow(context)
                await query.message.reply_text('Product removed. Use /myproducts to manage another listing.')
                return ConversationHandler.END
            if draft['phase'] == 'menu' and action == 'photo':
                product = await self._management_product(update.effective_user.id, pid)
                context.user_data.update(pending_product_id=pid,
                    pending_product_number=product['seller_number'],
                    pending_photo_revision=(product['photo_object_key'], product['photo_version_id']))
                await self._management_prompt(update, context, dict(draft, phase='photo'),
                    'Send one photo up to 5 MB, or /skip. A valid upload replaces the current photo; '
                    'the existing photo stays until the new one is saved.',
                    [[('Back', 'back'), ('Cancel', 'cancel')]])
                return PM_PHOTO
            raise ValueError
        except OrderError as exc:
            await query.message.reply_text(str(exc))
            try:
                return await self.management_home(update, context, draft['pid'])
            except OrderError:
                self.reset_flow(context)
                return ConversationHandler.END
        except ValueError:
            await query.message.reply_text('Invalid management action. Use the current buttons.')
            return None
        except Exception:
            await self.failure(update)
            return None
