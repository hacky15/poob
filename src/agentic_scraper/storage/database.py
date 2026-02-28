"""SQLite database initialization and schema management."""

from __future__ import annotations

from pathlib import Path

import aiosqlite


async def init_database(path: Path) -> aiosqlite.Connection:
    """Create the database file (and parent dirs) and initialize the schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await init_schema(conn)
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Create all tables if they don't exist."""
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            site TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            price REAL,
            currency TEXT NOT NULL DEFAULT 'USD',
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            seller_name TEXT NOT NULL DEFAULT '',
            image_urls TEXT NOT NULL DEFAULT '[]',
            listing_url TEXT NOT NULL DEFAULT '',
            posted_at TEXT,
            scraped_at TEXT NOT NULL,
            raw_data TEXT NOT NULL DEFAULT '{}',
            UNIQUE(site, external_id)
        );

        CREATE TABLE IF NOT EXISTS watch_items (
            id TEXT PRIMARY KEY,
            keywords TEXT NOT NULL,
            max_price REAL,
            location TEXT,
            radius_miles INTEGER,
            category TEXT,
            sites TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            discord_user_id TEXT NOT NULL DEFAULT '',
            discord_channel_id TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS deals (
            id TEXT PRIMARY KEY,
            listing_id TEXT NOT NULL,
            watch_item_id TEXT,
            score TEXT NOT NULL DEFAULT 'unknown',
            estimated_market_price REAL,
            discount_pct REAL,
            llm_reasoning TEXT NOT NULL DEFAULT '',
            notified INTEGER NOT NULL DEFAULT 0,
            notified_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scan_logs (
            id TEXT PRIMARY KEY,
            site TEXT NOT NULL,
            query_keywords TEXT NOT NULL DEFAULT '',
            listings_found INTEGER NOT NULL DEFAULT 0,
            deals_found INTEGER NOT NULL DEFAULT 0,
            errors TEXT NOT NULL DEFAULT '[]',
            duration_seconds REAL NOT NULL DEFAULT 0.0,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_listings_site_external
            ON listings(site, external_id);
        CREATE INDEX IF NOT EXISTS idx_watch_items_user
            ON watch_items(discord_user_id);
        CREATE INDEX IF NOT EXISTS idx_watch_items_active
            ON watch_items(is_active);
        CREATE INDEX IF NOT EXISTS idx_deals_notified
            ON deals(notified);
        CREATE INDEX IF NOT EXISTS idx_deals_created
            ON deals(created_at);
        CREATE INDEX IF NOT EXISTS idx_scan_logs_started
            ON scan_logs(started_at);

        CREATE TABLE IF NOT EXISTS user_preferences (
            id TEXT PRIMARY KEY,
            discord_user_id TEXT NOT NULL,
            preference_key TEXT NOT NULL,
            preference_value TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(discord_user_id, preference_key)
        );

        CREATE INDEX IF NOT EXISTS idx_user_prefs_user
            ON user_preferences(discord_user_id);

        CREATE TABLE IF NOT EXISTS conversation_messages (
            id TEXT PRIMARY KEY,
            discord_user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_call_id TEXT,
            timestamp TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_conv_msgs_user_time
            ON conversation_messages(discord_user_id, timestamp);
        """
    )
    await conn.commit()
