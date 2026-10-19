"""Upload/attach/compensation and removal cleanup shared by both interfaces.

Each function runs to completion in one worker, even if the requesting task is
cancelled. Database transactions never span Object Storage calls.
"""
import logging

LOG = logging.getLogger(__name__)


def save_product_photo(store, media, uid, pid, raw, expected_key, expected_version):
    key, version = media.put_product_photo(uid, pid, raw)
    try:
        previous = store.replace_seller_product_photo(uid, pid, key, version,
                                                       expected_key, expected_version)
    except Exception:
        try:
            media.delete_photo(key, version)
        except Exception as exc:
            LOG.error('Unattached photo cleanup failed product_id=%s category=%s', pid, type(exc).__name__)
        raise
    if previous[0]:
        try:
            media.delete_photo(*previous)
        except Exception as exc:
            LOG.error('Replaced photo cleanup failed product_id=%s category=%s', pid, type(exc).__name__)
    return key, version


def remove_product(store, media, uid, pid, expected=None):
    key, version = store.remove_seller_product(uid, pid, expected)
    if not key:
        return
    try:
        if media is None:
            raise RuntimeError('Photo storage unavailable')
        media.delete_photo(key, version)
        if version:
            store.clear_removed_product_photo(uid, pid, key, version)
        else:
            # A delete marker cannot establish that an unknown stored version's
            # bytes were removed. Retain its reference for operator follow-up.
            LOG.warning('Removed photo needs version cleanup product_id=%s', pid)
    except Exception as exc:
        LOG.error('Removed photo cleanup pending product_id=%s category=%s', pid, type(exc).__name__)
