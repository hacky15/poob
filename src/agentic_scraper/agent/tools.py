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
    scan_log_repo: Any = None,
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
        scan_log_repo: ScanLogRepository instance (or mock).

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
        """Add an interest to the watchlist so the patrol system looks out for it.

        Args:
            item_name: What to look out for (e.g. "coffee table", "PS5").
            max_price: Maximum price in dollars, or None for no limit.
            priority: "low", "normal", or "high".
            category: Item category like "furniture", "electronics", "clothing".
        """
        # Create a WatchItem so the patrol system matches against it
        watch_item = WatchItem(
            interest=item_name,
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
        """Remove an interest from the watchlist and stop looking out for it."""
        items = await watchlist_repo.list_for_user(discord_user_id)
        matched = None
        for item in items:
            if item.interest.lower() == item_name.lower() and item.is_active:
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
        """Trigger an immediate marketplace patrol cycle."""
        await scheduler.trigger_now()
        return "Patrol triggered. The system will sweep Marketplace shortly."

    # ------------------------------------------------------------------
    # pause_patrol
    # ------------------------------------------------------------------
    async def _pause_patrol() -> str:
        """Pause the automatic patrol scheduler. Scanning will stop until resumed."""
        scheduler.pause()
        return "Patrol paused. The scanner will stop sweeping until you resume it."

    # ------------------------------------------------------------------
    # resume_patrol
    # ------------------------------------------------------------------
    async def _resume_patrol() -> str:
        """Resume the automatic patrol scheduler after it was paused."""
        scheduler.resume()
        return "Patrol resumed. The scanner is sweeping Marketplace again."

    # ------------------------------------------------------------------
    # get_recent_deals
    # ------------------------------------------------------------------
    async def _get_recent_deals(count: int = 10) -> str:
        """Get recent deals found by the patrol system with details."""
        deals = await deal_repo.list_recent(count)
        if not deals:
            return "No recent deals found."

        lines = []
        for i, deal in enumerate(deals, 1):
            listing = await listing_repo.get(deal.listing_id)
            title = listing.title if listing else "Unknown"
            price = f"${listing.price:.0f}" if listing and listing.price else "N/A"
            discount = f"{deal.discount_pct:.0f}% off" if deal.discount_pct else ""
            score = deal.score.value.upper()

            line = f"{i}. {title} -- {price} ({score} deal"
            if discount:
                line += f", ~{discount}"
            line += ")"

            # Add location and URL
            if listing:
                details = []
                if listing.location:
                    details.append(f"Location: {listing.location}")
                if listing.listing_url:
                    details.append(f"URL: {listing.listing_url}")
                if details:
                    line += f"\n   {' | '.join(details)}"

            # Add reasoning snippet
            if deal.llm_reasoning:
                reason = deal.llm_reasoning[:120]
                if len(deal.llm_reasoning) > 120:
                    reason += "..."
                line += f"\n   Why: {reason}"

            lines.append(line)

        return "Recent deals:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # get_deal_details
    # ------------------------------------------------------------------
    async def _get_deal_details(deal_number: int = 1) -> str:
        """Get full details about a specific deal including reasoning.

        Args:
            deal_number: Which deal to show (1 = most recent, 2 = second most recent, etc).
        """
        deals = await deal_repo.list_recent(deal_number)
        if not deals or deal_number < 1 or deal_number > len(deals):
            return f"Deal #{deal_number} not found. Try get_recent_deals first."

        deal = deals[deal_number - 1]
        listing = await listing_repo.get(deal.listing_id)

        parts = [f"Deal #{deal_number} Details:"]

        if listing:
            parts.append(f"Title: {listing.title}")
            if listing.price is not None:
                parts.append(f"Price: ${listing.price:.2f}")
            if listing.location:
                parts.append(f"Location: {listing.location}")
            if listing.description:
                parts.append(f"Description: {listing.description[:300]}")
            if listing.seller_name:
                parts.append(f"Seller: {listing.seller_name}")
            if listing.listing_url:
                parts.append(f"URL: {listing.listing_url}")
            if listing.image_urls:
                parts.append(f"Images: {len(listing.image_urls)} photo(s)")

        parts.append(f"Deal Score: {deal.score.value.upper()}")
        if deal.estimated_market_price:
            parts.append(f"Estimated Market Price: ${deal.estimated_market_price:.2f}")
        if deal.discount_pct:
            parts.append(f"Discount: {deal.discount_pct:.0f}% below market")
        if deal.llm_reasoning:
            parts.append(f"Why it's a deal: {deal.llm_reasoning}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # search_listings
    # ------------------------------------------------------------------
    async def _search_listings(
        keyword: str | None = None,
        max_price: float | None = None,
        min_price: float | None = None,
        count: int = 10,
    ) -> str:
        """Search recent marketplace listings by keyword and/or price range.

        Args:
            keyword: Search term to match in listing titles.
            max_price: Maximum price filter in dollars.
            min_price: Minimum price filter in dollars.
            count: Number of results to return (default 10).
        """
        listings = await listing_repo.search(
            keyword=keyword, max_price=max_price, min_price=min_price, limit=count,
        )

        if not listings:
            parts = ["No listings found"]
            if keyword:
                parts.append(f"matching '{keyword}'")
            if min_price is not None or max_price is not None:
                price_parts = []
                if min_price is not None:
                    price_parts.append(f"${min_price:.0f}")
                if max_price is not None:
                    price_parts.append(f"${max_price:.0f}")
                parts.append(f"in range {'-'.join(price_parts)}")
            return " ".join(parts) + "."

        lines = []
        for listing in listings:
            price = f"${listing.price:.0f}" if listing.price else "N/A"
            line = f"- {listing.title} -- {price}"
            if listing.location:
                line += f" ({listing.location})"
            if listing.listing_url:
                line += f"\n  URL: {listing.listing_url}"
            lines.append(line)

        header = f"Found {len(listings)} listing(s)"
        if keyword:
            header += f" matching '{keyword}'"
        return f"{header}:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # get_preferences
    # ------------------------------------------------------------------
    async def _get_preferences() -> str:
        """Show the user's current preferences and settings."""
        all_prefs = await prefs_repo.get_all(discord_user_id)

        if not all_prefs:
            return "No preferences set yet. You can set your location, search radius, and more."

        parts = ["Your current settings:"]

        # Location
        loc_json = all_prefs.get("location")
        if loc_json:
            loc = json.loads(loc_json)
            city = loc.get("city", "Not set")
            radius = loc.get("radius_miles")
            loc_str = city
            if radius:
                loc_str += f" ({radius} mile radius)"
            parts.append(f"Location: {loc_str}")
        else:
            parts.append("Location: Not set")

        # Search priorities
        prio_json = all_prefs.get("search_priorities")
        if prio_json:
            prio = json.loads(prio_json)
            prio_items = []
            if prio.get("just_listed_first"):
                prio_items.append("Just-listed first: yes")
            if prio.get("desperate_seller_detection"):
                prio_items.append("Desperate seller detection: yes")
            if "min_deal_score" in prio:
                prio_items.append(f"Min deal score: {prio['min_deal_score']}")
            if prio_items:
                parts.append("Search priorities: " + ", ".join(prio_items))
        else:
            parts.append("Search priorities: Default")

        # Wishlist summary
        wl_json = all_prefs.get("wishlist")
        if wl_json:
            wl = json.loads(wl_json)
            parts.append(f"Wishlist: {len(wl)} item(s)")
        else:
            parts.append("Wishlist: Empty")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # get_scan_history
    # ------------------------------------------------------------------
    async def _get_scan_history(hours: int = 24) -> str:
        """Show recent scan statistics and activity summary.

        Args:
            hours: Number of hours to look back (default 24).
        """
        if scan_log_repo is None:
            return "Scan history not available."

        stats = await scan_log_repo.get_stats(hours)
        recent_logs = await scan_log_repo.list_recent(5)

        parts = [f"Scan activity (last {hours}h):"]
        parts.append(f"Total patrols: {stats['total_scans']}")
        parts.append(f"Listings found: {stats['total_listings']}")
        parts.append(f"Deals found: {stats['total_deals']}")
        if stats["total_errors"] > 0:
            parts.append(f"Errors: {stats['total_errors']}")
        if stats["total_scans"] > 0:
            parts.append(f"Avg cycle duration: {stats['avg_duration']}s")

        if recent_logs:
            parts.append("\nRecent patrols:")
            for log in recent_logs[:5]:
                time_str = log.started_at.strftime("%H:%M")
                line = (
                    f"  {time_str} - {log.listings_found} listings,"
                    f" {log.deals_found} deals ({log.duration_seconds:.0f}s)"
                )
                if log.errors:
                    line += f" [{len(log.errors)} error(s)]"
                parts.append(line)

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # get_scanner_status
    # ------------------------------------------------------------------
    async def _get_scanner_status() -> str:
        """Get the current status of the marketplace patrol system."""
        status = "patrolling" if scheduler.is_running else "stopped"
        if scheduler.is_paused:
            status = "paused"

        parts = [f"Patrol status: {status}."]
        if scheduler.last_scan_time:
            parts.append(f"Last patrol: {scheduler.last_scan_time}")
        if scheduler.next_scan_time:
            parts.append(f"Next patrol: {scheduler.next_scan_time}")

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
                "Add an interest to the user's watchlist. The patrol system will"
                " look out for matching deals on Marketplace."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_remove_from_wishlist,
            name="remove_from_wishlist",
            description="Remove an interest from the watchlist and stop looking out for it.",
        ),
        StructuredTool.from_function(
            coroutine=_show_wishlist,
            name="show_wishlist",
            description="Show all items the user is currently looking out for.",
        ),
        StructuredTool.from_function(
            coroutine=_trigger_scan,
            name="trigger_scan",
            description="Trigger an immediate patrol cycle to sweep Marketplace for deals.",
        ),
        StructuredTool.from_function(
            coroutine=_pause_patrol,
            name="pause_patrol",
            description="Pause the automatic patrol scheduler. Scanning stops until resumed.",
        ),
        StructuredTool.from_function(
            coroutine=_resume_patrol,
            name="resume_patrol",
            description="Resume the automatic patrol scheduler after it was paused.",
        ),
        StructuredTool.from_function(
            coroutine=_get_recent_deals,
            name="get_recent_deals",
            description=(
                "Get recent deals found by the patrol system with title, price,"
                " deal score, location, URL, and reasoning."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_get_deal_details,
            name="get_deal_details",
            description=(
                "Get full details about a specific deal including description,"
                " images, market price, and why it's a good deal."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_search_listings,
            name="search_listings",
            description=(
                "Search recent marketplace listings by keyword and/or price range."
                " Use this when the user wants to browse or find specific items."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_get_scanner_status,
            name="get_scanner_status",
            description="Get the current status of the marketplace patrol system.",
        ),
        StructuredTool.from_function(
            coroutine=_get_preferences,
            name="get_preferences",
            description=(
                "Show the user's current preferences including location,"
                " search radius, search priorities, and wishlist summary."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_get_scan_history,
            name="get_scan_history",
            description=(
                "Show recent scan statistics: total patrols, listings found,"
                " deals found, errors, and recent patrol details."
            ),
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
