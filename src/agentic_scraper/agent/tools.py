"""Agent tool definitions for the conversational deal-hunting agent."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from agentic_scraper.storage.models import WatchItem


def build_tools(
    *,
    discord_user_id: str,
    discord_channel_id: str,
    prefs_repo: Any,
    watchlist_repo: Any,
    deal_repo: Any,
    listing_repo: Any,
    scheduler: Any,
) -> list[StructuredTool]:
    """Build agent tools scoped to a specific user and channel.

    Each tool closes over the user ID and repository instances so the LLM
    never sees internal identifiers.

    Args:
        discord_user_id: The Discord user these tools act on behalf of.
        discord_channel_id: The channel the conversation is happening in.
        prefs_repo: UserPreferencesRepository instance.
        watchlist_repo: WatchlistRepository instance.
        deal_repo: DealRepository instance (or mock).
        listing_repo: ListingRepository instance (or mock).
        scheduler: ScanScheduler instance (or mock).

    Returns:
        List of LangChain StructuredTool instances.
    """

    # ------------------------------------------------------------------
    # add_to_wishlist
    # ------------------------------------------------------------------
    async def _add_to_wishlist(
        item_name: str,
        max_price: float | None = None,
        priority: str = "normal",
        category: str | None = None,
    ) -> str:
        """Add an item to the wishlist and create a watch item for the scanner.

        Args:
            item_name: What to search for (e.g. "coffee table", "PS5").
            max_price: Maximum price in dollars, or None for no limit.
            priority: "low", "normal", or "high".
            category: Item category like "furniture", "electronics", "clothing".
                Helps filter out irrelevant results (e.g. "coffee table" without
                category returns books about coffee tables).
        """
        # Create a real WatchItem so the existing ScanEngine picks it up
        watch_item = WatchItem(
            keywords=item_name,
            max_price=max_price,
            category=category,
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
        )
        await watchlist_repo.save(watch_item)

        # Persist in user preferences for display / prompt injection
        wishlist_json = await prefs_repo.get(discord_user_id, "wishlist")
        wishlist: list[dict] = json.loads(wishlist_json) if wishlist_json else []
        entry: dict[str, Any] = {"name": item_name, "priority": priority}
        if max_price is not None:
            entry["max_price"] = max_price
        if category is not None:
            entry["category"] = category
        wishlist.append(entry)
        await prefs_repo.set(discord_user_id, "wishlist", json.dumps(wishlist))

        price_str = f" (max ${max_price})" if max_price else ""
        return f"Added '{item_name}'{price_str} to your wishlist."

    # ------------------------------------------------------------------
    # remove_from_wishlist
    # ------------------------------------------------------------------
    async def _remove_from_wishlist(item_name: str) -> str:
        """Remove an item from the wishlist and deactivate its watch item."""
        items = await watchlist_repo.list_for_user(discord_user_id)
        matched = None
        for item in items:
            if item.keywords.lower() == item_name.lower() and item.is_active:
                matched = item
                break

        if matched is None:
            return f"Item '{item_name}' not found on your wishlist."

        await watchlist_repo.deactivate(matched.id)

        # Remove from preferences
        wishlist_json = await prefs_repo.get(discord_user_id, "wishlist")
        if wishlist_json:
            wishlist = json.loads(wishlist_json)
            wishlist = [w for w in wishlist if w["name"].lower() != item_name.lower()]
            await prefs_repo.set(discord_user_id, "wishlist", json.dumps(wishlist))

        return f"Removed '{item_name}' from your wishlist."

    # ------------------------------------------------------------------
    # show_wishlist
    # ------------------------------------------------------------------
    async def _show_wishlist() -> str:
        """Show all items currently on the wishlist."""
        wishlist_json = await prefs_repo.get(discord_user_id, "wishlist")
        if not wishlist_json:
            return "Your wishlist is empty."

        wishlist = json.loads(wishlist_json)
        if not wishlist:
            return "Your wishlist is empty."

        lines = []
        for item in wishlist:
            line = f"- {item['name']}"
            if "max_price" in item:
                line += f" (max ${item['max_price']})"
            if item.get("priority", "normal") != "normal":
                line += f" [{item['priority']}]"
            lines.append(line)

        return "Your wishlist:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # trigger_scan
    # ------------------------------------------------------------------
    async def _trigger_scan() -> str:
        """Trigger an immediate marketplace scan."""
        await scheduler.trigger_now()
        return "Scan triggered. The scanner will run shortly."

    # ------------------------------------------------------------------
    # get_recent_deals
    # ------------------------------------------------------------------
    async def _get_recent_deals(count: int = 10) -> str:
        """Get recent deals found by the scanner."""
        deals = await deal_repo.list_recent(count)
        if not deals:
            return "No recent deals found."

        lines = []
        for deal in deals:
            listing = await listing_repo.get(deal.listing_id)
            title = listing.title if listing else "Unknown"
            price = f"${listing.price}" if listing and listing.price else "N/A"
            discount = f"{deal.discount_pct:.0f}% off" if deal.discount_pct else ""
            lines.append(
                f"- {title} -- {price} ({deal.score.value} deal"
                + (f", ~{discount}" if discount else "")
                + ")"
            )

        return "Recent deals:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # get_scanner_status
    # ------------------------------------------------------------------
    async def _get_scanner_status() -> str:
        """Get the current status of the marketplace scanner."""
        status = "running" if scheduler.is_running else "stopped"
        if scheduler.is_paused:
            status = "paused"

        parts = [f"Scanner status: {status}."]
        if scheduler.last_scan_time:
            parts.append(f"Last scan: {scheduler.last_scan_time}")
        if scheduler.next_scan_time:
            parts.append(f"Next scan: {scheduler.next_scan_time}")

        return " ".join(parts)

    # ------------------------------------------------------------------
    # update_preferences
    # ------------------------------------------------------------------
    async def _update_preferences(
        location: str | None = None,
        radius_miles: int | None = None,
        just_listed_first: bool | None = None,
        desperate_seller_detection: bool | None = None,
        min_deal_score: str | None = None,
    ) -> str:
        """Update user search preferences."""
        updated: list[str] = []

        if location is not None or radius_miles is not None:
            loc_json = await prefs_repo.get(discord_user_id, "location")
            loc = json.loads(loc_json) if loc_json else {}
            if location is not None:
                loc["city"] = location
            if radius_miles is not None:
                loc["radius_miles"] = radius_miles
            await prefs_repo.set(discord_user_id, "location", json.dumps(loc))
            updated.append("location")

        if any(
            v is not None
            for v in [just_listed_first, desperate_seller_detection, min_deal_score]
        ):
            prio_json = await prefs_repo.get(discord_user_id, "search_priorities")
            prio = json.loads(prio_json) if prio_json else {}
            if just_listed_first is not None:
                prio["just_listed_first"] = just_listed_first
            if desperate_seller_detection is not None:
                prio["desperate_seller_detection"] = desperate_seller_detection
            if min_deal_score is not None:
                prio["min_deal_score"] = min_deal_score
            await prefs_repo.set(
                discord_user_id, "search_priorities", json.dumps(prio)
            )
            updated.append("search priorities")

        if not updated:
            return "No preferences updated."
        return f"Updated {', '.join(updated)}."

    # ------------------------------------------------------------------
    # Assemble and return
    # ------------------------------------------------------------------
    return [
        StructuredTool.from_function(
            coroutine=_add_to_wishlist,
            name="add_to_wishlist",
            description=(
                "Add an item to the user's wishlist. Creates a watch item so the"
                " scanner will look for deals on this item."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_remove_from_wishlist,
            name="remove_from_wishlist",
            description="Remove an item from the user's wishlist and stop scanning for it.",
        ),
        StructuredTool.from_function(
            coroutine=_show_wishlist,
            name="show_wishlist",
            description="Show all items currently on the user's wishlist.",
        ),
        StructuredTool.from_function(
            coroutine=_trigger_scan,
            name="trigger_scan",
            description="Trigger an immediate marketplace scan for all wishlist items.",
        ),
        StructuredTool.from_function(
            coroutine=_get_recent_deals,
            name="get_recent_deals",
            description="Get recent deals found by the scanner, with listing details.",
        ),
        StructuredTool.from_function(
            coroutine=_get_scanner_status,
            name="get_scanner_status",
            description="Get the current status of the marketplace scanner.",
        ),
        StructuredTool.from_function(
            coroutine=_update_preferences,
            name="update_preferences",
            description=(
                "Update user preferences like location, search radius,"
                " and search priority settings."
            ),
        ),
    ]
