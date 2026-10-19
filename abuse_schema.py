"""Durable admission ledger; Oracle equivalent is in db/schema.sql."""
TABLE_COLUMNS = {'ABUSE_LIMITS': ('telegram_user_id', 'window_start', 'listing_count',
                 'order_count', 'photo_attempt_count', 'last_listing_at', 'last_order_at',
                 'last_photo_at', 'blocked_until')}

SCHEMA = """
CREATE TABLE IF NOT EXISTS abuse_limits (
    telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
    window_start TEXT NOT NULL,
    listing_count INTEGER DEFAULT 0 NOT NULL CHECK (listing_count >= 0),
    order_count INTEGER DEFAULT 0 NOT NULL CHECK (order_count >= 0),
    photo_attempt_count INTEGER DEFAULT 0 NOT NULL CHECK (photo_attempt_count >= 0),
    last_listing_at TEXT,
    last_order_at TEXT,
    last_photo_at TEXT,
    blocked_until TEXT
);
"""
