"""Repository for ScanLog CRUD operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from poob.storage.models import ScanLog


class ScanLogRepository:
    """CRUD operations for scan audit logs."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(self, log: ScanLog) -> ScanLog:
        """Insert a scan log. Assigns an ID if not set."""
        if log.id is None:
            log.id = str(uuid.uuid4())

        await self._conn.execute(
            """
            INSERT INTO scan_logs
                (id, site, category, listings_found, deals_found,
                 errors, duration_seconds, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.id,
                log.site,
                log.category,
                log.listings_found,
                log.deals_found,
                json.dumps(log.errors),
                log.duration_seconds,
                log.started_at.isoformat(),
                log.completed_at.isoformat() if log.completed_at else None,
            ),
        )
        await self._conn.commit()
        return log

    async def get(self, log_id: str) -> ScanLog | None:
        """Fetch a scan log by ID."""
        cursor = await self._conn.execute(
            "SELECT * FROM scan_logs WHERE id = ?", (log_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_scan_log(row)

    async def list_recent(self, limit: int = 20) -> list[ScanLog]:
        """List the most recent scan logs."""
        cursor = await self._conn.execute(
            "SELECT * FROM scan_logs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_scan_log(row) for row in rows]

    async def get_stats(self, hours: int = 24) -> dict:
        """Get aggregate scan statistics for the last N hours.

        Args:
            hours: Number of hours to look back.

        Returns:
            Dict with total_scans, total_listings, total_deals,
            total_errors, avg_duration.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cursor = await self._conn.execute(
            """
            SELECT
                COUNT(*) as total_scans,
                COALESCE(SUM(listings_found), 0) as total_listings,
                COALESCE(SUM(deals_found), 0) as total_deals,
                COALESCE(AVG(duration_seconds), 0.0) as avg_duration
            FROM scan_logs
            WHERE started_at >= ?
            """,
            (cutoff,),
        )
        row = await cursor.fetchone()

        # Count errors separately (stored as JSON array)
        err_cursor = await self._conn.execute(
            "SELECT errors FROM scan_logs WHERE started_at >= ?",
            (cutoff,),
        )
        err_rows = await err_cursor.fetchall()
        total_errors = sum(
            len(json.loads(r["errors"])) for r in err_rows if r["errors"]
        )

        return {
            "total_scans": row["total_scans"],
            "total_listings": row["total_listings"],
            "total_deals": row["total_deals"],
            "total_errors": total_errors,
            "avg_duration": round(row["avg_duration"], 1),
        }

    @staticmethod
    def _row_to_scan_log(row: aiosqlite.Row) -> ScanLog:
        """Convert a database row to a ScanLog dataclass."""
        return ScanLog(
            id=row["id"],
            site=row["site"],
            category=row["category"],
            listings_found=row["listings_found"],
            deals_found=row["deals_found"],
            errors=json.loads(row["errors"]),
            duration_seconds=row["duration_seconds"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
        )
