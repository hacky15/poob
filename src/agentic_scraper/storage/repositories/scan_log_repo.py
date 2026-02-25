"""Repository for ScanLog CRUD operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import aiosqlite

from agentic_scraper.storage.models import ScanLog


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
                (id, site, query_keywords, listings_found, deals_found,
                 errors, duration_seconds, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.id,
                log.site,
                log.query_keywords,
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

    @staticmethod
    def _row_to_scan_log(row: aiosqlite.Row) -> ScanLog:
        """Convert a database row to a ScanLog dataclass."""
        return ScanLog(
            id=row["id"],
            site=row["site"],
            query_keywords=row["query_keywords"],
            listings_found=row["listings_found"],
            deals_found=row["deals_found"],
            errors=json.loads(row["errors"]),
            duration_seconds=row["duration_seconds"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
        )
