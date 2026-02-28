"""Repository for Listing CRUD operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from agentic_scraper.storage.models import Listing


class ListingRepository:
    """CRUD operations for marketplace listings."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(self, listing: Listing) -> Listing:
        """Insert or update a listing. Assigns an ID if not set.

        Uses ON CONFLICT to update existing listings (matched by site + external_id)
        with fresh data from a new scan.
        """
        if listing.id is None:
            listing.id = str(uuid.uuid4())

        await self._conn.execute(
            """
            INSERT INTO listings
                (id, site, external_id, title, price, currency, description,
                 location, seller_name, image_urls, listing_url, posted_at,
                 scraped_at, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site, external_id) DO UPDATE SET
                title = excluded.title,
                price = excluded.price,
                location = excluded.location,
                seller_name = excluded.seller_name,
                image_urls = excluded.image_urls,
                listing_url = excluded.listing_url,
                scraped_at = excluded.scraped_at,
                raw_data = excluded.raw_data
            """,
            (
                listing.id,
                listing.site,
                listing.external_id,
                listing.title,
                listing.price,
                listing.currency,
                listing.description,
                listing.location,
                listing.seller_name,
                json.dumps(listing.image_urls),
                listing.listing_url,
                listing.posted_at.isoformat() if listing.posted_at else None,
                listing.scraped_at.isoformat(),
                json.dumps(listing.raw_data),
            ),
        )
        await self._conn.commit()
        return listing

    async def get(self, listing_id: str) -> Listing | None:
        """Fetch a listing by ID."""
        cursor = await self._conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_listing(row)

    async def exists(self, site: str, external_id: str) -> bool:
        """Check if a listing with this site+external_id already exists."""
        cursor = await self._conn.execute(
            "SELECT 1 FROM listings WHERE site = ? AND external_id = ?",
            (site, external_id),
        )
        return await cursor.fetchone() is not None

    async def list_recent(self, limit: int = 20) -> list[Listing]:
        """List the most recently scraped listings."""
        cursor = await self._conn.execute(
            "SELECT * FROM listings ORDER BY scraped_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_listing(row) for row in rows]

    @staticmethod
    def _row_to_listing(row: aiosqlite.Row) -> Listing:
        """Convert a database row to a Listing dataclass."""
        return Listing(
            id=row["id"],
            site=row["site"],
            external_id=row["external_id"],
            title=row["title"],
            price=row["price"],
            currency=row["currency"],
            description=row["description"],
            location=row["location"],
            seller_name=row["seller_name"],
            image_urls=json.loads(row["image_urls"]),
            listing_url=row["listing_url"],
            posted_at=(
                datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None
            ),
            scraped_at=datetime.fromisoformat(row["scraped_at"]),
            raw_data=json.loads(row["raw_data"]),
        )
