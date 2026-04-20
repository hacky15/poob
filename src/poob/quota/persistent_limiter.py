"""Persistent quota tracking that survives application restarts.

Uses pyrate-limiter with SQLiteBucket backend to persist API usage counts
in data/quota_state.db. Replaces the old in-memory _MonthlyQuota class
that re-burned Vision 1000/mo and SerpAPI 250/mo on every restart.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pyrate_limiter import Duration, Limiter, Rate, SQLiteBucket

from poob.utils.logging import get_logger

log = get_logger("quota.persistent_limiter")

# Default database path for quota state
_DEFAULT_DB_PATH = Path("data/quota_state.db")


class PersistentQuota:
    """SQLite-backed monthly quota tracker.

    Unlike the old in-memory tracker, this persists across restarts.
    The pyrate-limiter SQLiteBucket provides ACID-compliant rate limiting
    in a single file with zero infrastructure overhead.

    Args:
        name: Human-readable name for this quota (e.g. "google_cloud_vision").
        monthly_limit: Maximum allowed requests per month.
        db_path: Path to the SQLite database file for quota state.
    """

    def __init__(
        self,
        name: str,
        monthly_limit: int,
        db_path: Path = _DEFAULT_DB_PATH,
    ) -> None:
        self.name = name
        self._monthly_limit = monthly_limit
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a rate limiter with ~monthly bucket (30 days)
        rate = Rate(monthly_limit, Duration.DAY * 30)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        # Ensure the bucket table exists before constructing the limiter
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS '{name}' "
            f"(name VARCHAR, item_timestamp INTEGER)"
        )
        self._conn.execute(
            f"CREATE INDEX IF NOT EXISTS 'idx_{name}_ts' "
            f"ON '{name}' (item_timestamp)"
        )
        self._conn.commit()
        bucket = SQLiteBucket(
            rates=[rate],
            conn=self._conn,
            table=name,
        )
        self._limiter = Limiter(bucket)
        self._bucket = bucket

    def has_remaining(self) -> bool:
        """Check if quota has remaining capacity without consuming."""
        try:
            # Count items within the rate window — if below limit, we have room
            interval = self._bucket.rates[-1].interval
            now_ms = int(__import__("time").time() * 1000)
            cur = self._conn.execute(
                f"SELECT COUNT(*) FROM '{self.name}' "
                f"WHERE item_timestamp >= {now_ms} - {interval}"
            )
            count = cur.fetchone()[0]
            return count < self._monthly_limit
        except Exception:
            # On any error, assume quota available (fail-open)
            return True

    def try_acquire(self) -> bool:
        """Try to consume one quota unit. Returns True if successful.

        CRITICAL: Uses blocking=False to prevent pyrate-limiter from calling
        time.sleep() which would block the entire asyncio event loop.  With
        blocking=True (the default), exceeding the monthly quota causes a
        multi-day synchronous sleep that freezes all coroutines.
        """
        try:
            acquired = self._limiter.try_acquire(self.name, blocking=False)
            if not acquired:
                log.warning(
                    "Quota exhausted",
                    service=self.name,
                    limit=self._monthly_limit,
                )
                return False
            return True
        except Exception as exc:
            log.warning(
                "Quota check failed, allowing request",
                service=self.name,
                error=str(exc)[:100],
            )
            return True  # Fail-open

    @property
    def monthly_limit(self) -> int:
        """The configured monthly limit."""
        return self._monthly_limit


class PersistentKV:
    """Lightweight key-value store backed by the quota SQLite DB.

    Used to persist ephemeral service state (Vision API disabled flag,
    tiebreaker cooldown expiry) across restarts so the system doesn't
    repeat mistakes it already learned in a previous session.

    Args:
        db_path: Path to the quota SQLite database.
    """

    _TABLE = "kv_state"

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} "
            f"(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)"
        )
        self._conn.commit()

    def get(self, key: str) -> str | None:
        """Get a value by key. Returns None if not found."""
        cur = self._conn.execute(
            f"SELECT value FROM {self._TABLE} WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        """Set a key-value pair, replacing if exists."""
        import time
        self._conn.execute(
            f"INSERT OR REPLACE INTO {self._TABLE} (key, value, updated_at) "
            f"VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )
        self._conn.commit()

    def delete(self, key: str) -> None:
        """Delete a key."""
        self._conn.execute(
            f"DELETE FROM {self._TABLE} WHERE key = ?", (key,)
        )
        self._conn.commit()

    def get_float(self, key: str, default: float = 0.0) -> float:
        """Get a float value, returning default if not found or invalid."""
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def set_float(self, key: str, value: float) -> None:
        """Set a float value."""
        self.set(key, str(value))

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean value."""
        raw = self.get(key)
        if raw is None:
            return default
        return raw.lower() == "true"

    def set_bool(self, key: str, value: bool) -> None:
        """Set a boolean value."""
        self.set(key, "true" if value else "false")


def build_quotas(
    db_path: Path = _DEFAULT_DB_PATH,
    vision_limit: int = 1000,
    serpapi_limit: int = 250,
) -> dict[str, PersistentQuota]:
    """Build all persistent quota trackers.

    Args:
        db_path: Path to the quota SQLite database.
        vision_limit: Google Cloud Vision monthly limit.
        serpapi_limit: SerpAPI monthly limit.

    Returns:
        Dict mapping quota name to PersistentQuota instance.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    quotas = {
        "google_cloud_vision": PersistentQuota(
            "google_cloud_vision", vision_limit, db_path
        ),
        "serpapi": PersistentQuota("serpapi", serpapi_limit, db_path),
    }
    log.info(
        "Persistent quotas initialized",
        db_path=str(db_path),
        vision_limit=vision_limit,
        serpapi_limit=serpapi_limit,
    )
    return quotas
