"""Agent tool definitions for the conversational deal-hunting agent."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from agentic_scraper.storage.models import ExclusionItem, WatchItem


# Map common synonyms to the canonical notification levels.
# LLMs frequently say "excellent" or "amazing" instead of "great"/"incredible".
_LEVEL_ALIASES: dict[str, str] = {
    "all": "all",
    "any": "all",
    "everything": "all",
    "good": "good",
    "decent": "good",
    "great": "great",
    "excellent": "great",
    "amazing": "incredible",
    "incredible": "incredible",
    "insane": "incredible",
    "free": "free",
}

_VALID_LEVELS = {"all", "good", "great", "incredible", "free"}


def _normalize_notification_level(raw: str) -> str | None:
    """Normalize a notification level string to one of the valid values.

    Returns the canonical level, or None if the input is unrecognizable.
    """
    key = raw.strip().lower()
    if key in _VALID_LEVELS:
        return key
    return _LEVEL_ALIASES.get(key)


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
    exclusion_repo: Any = None,
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
        notification_level: str = "good",
        priority: str = "normal",
        category: str | None = None,
        notes: str = "",
        effort: str = "normal",
        search_configs: str = "[]",
    ) -> str:
        """Add an interest to the watchlist so the patrol system looks out for it.

        IMPORTANT: You MUST gather all required info before calling this tool:
        - item_name (required)
        - condition preference (required — ask user if not stated)
        - location(s) + radius (required — ask user if not stated)
        - max_price (optional but recommended)

        Args:
            item_name: What to look out for (e.g. "coffee table", "PS5").
            max_price: Maximum price in dollars, or None for no limit.
            notification_level: When to notify. Options:
                "all" = every matching listing regardless of deal quality,
                "good" = good deals and above (20%+ below market),
                "great" = great deals and above (40%+ below market),
                "incredible" = only incredible deals (50%+ below market),
                "free" = only free listings ($0).
            priority: "low", "normal", or "high".
            category: Item category like "furniture", "electronics", "clothing".
            notes: User preferences for this item (e.g. "not metal", "modern style",
                "ideally wood finish"). Passed to the deal evaluator as context.
            effort: Evaluation thoroughness. "normal" = standard pipeline,
                "max" = all listing images sent to VLM, best providers used,
                no unbranded value cap, OCR text extracted from every image.
                Use "max" for high-value or hard-to-evaluate items the user
                really cares about (TVs, electronics, appliances, etc.).
            search_configs: JSON string of search configurations. Each config is a dict
                with optional keys:
                - "location": FB city slug (e.g. "madison", "appleton", "green-bay")
                - "radius_miles": search radius in miles (e.g. 20, 40)
                - "condition": FB condition filter. Values:
                    "new", "used_like_new", "used_good", "used_fair"
                    Combine with comma: "used_good,used_like_new"
                    Omit for any condition.
                - "min_price": minimum price in dollars
                - "max_price": maximum price for THIS search (overrides top-level)
                Example: '[{"location": "madison", "radius_miles": 20, "condition": "used_good", "max_price": 200}]'
                Multiple configs = multiple searches per patrol cycle.
        """
        # Validate and normalize notification_level
        resolved = _normalize_notification_level(notification_level)
        if resolved is None:
            return (
                f"Invalid notification level '{notification_level}'. "
                f"Valid options: all, good, great, incredible, free."
            )
        notification_level = resolved

        # Validate effort
        valid_efforts = {"normal", "max"}
        if effort not in valid_efforts:
            effort = "normal"

        # Parse search_configs from JSON string
        try:
            configs = json.loads(search_configs) if search_configs else []
        except (json.JSONDecodeError, TypeError):
            configs = []

        # Check for existing active item with the same name (prevent duplicates)
        existing_items = await watchlist_repo.list_for_user(discord_user_id)
        existing = None
        for item in existing_items:
            if item.interest.lower() == item_name.lower() and item.is_active:
                existing = item
                break

        if existing:
            # Update the existing item instead of creating a duplicate
            existing.max_price = max_price
            existing.notification_threshold = notification_level
            existing.effort = effort
            if category:
                existing.category = category
            if notes:
                existing.notes = notes
            if configs:
                existing.search_configs = configs
            await watchlist_repo.save(existing)
            watch_item = existing
        else:
            watch_item = WatchItem(
                interest=item_name,
                max_price=max_price,
                notification_threshold=notification_level,
                category=category,
                effort=effort,
                discord_user_id=discord_user_id,
                discord_channel_id=discord_channel_id,
                notes=notes,
                search_configs=configs,
            )
            await watchlist_repo.save(watch_item)

        # Sync preferences JSON (used for system prompt injection)
        wishlist_json = await prefs_repo.get(discord_user_id, "wishlist")
        wishlist: list[dict] = json.loads(wishlist_json) if wishlist_json else []
        entry: dict[str, Any] = {
            "name": item_name,
            "priority": priority,
            "notification_level": notification_level,
            "effort": effort,
        }
        if max_price is not None:
            entry["max_price"] = max_price
        if category is not None:
            entry["category"] = category
        if notes:
            entry["notes"] = notes
        if configs:
            entry["search_configs"] = configs

        # Replace existing entry or append new one
        replaced = False
        for i, w in enumerate(wishlist):
            if w["name"].lower() == item_name.lower():
                wishlist[i] = entry
                replaced = True
                break
        if not replaced:
            wishlist.append(entry)
        await prefs_repo.set(discord_user_id, "wishlist", json.dumps(wishlist))

        price_str = f" (max ${max_price})" if max_price else ""
        level_str = f", notify for {notification_level} deals"
        effort_str = " [MAX EFFORT]" if effort == "max" else ""
        notes_str = f" ({notes})" if notes else ""
        configs_str = ""
        if configs:
            cfg_parts = []
            for c in configs:
                parts = []
                if c.get("location"):
                    parts.append(c["location"])
                if c.get("radius_miles"):
                    parts.append(f"{c['radius_miles']}mi")
                if c.get("condition"):
                    parts.append(c["condition"].replace("used_", ""))
                if c.get("max_price") is not None:
                    parts.append(f"max ${c['max_price']}")
                cfg_parts.append(" ".join(parts))
            configs_str = f"\nSearches: {' | '.join(cfg_parts)}"
        return f"Added '{item_name}'{price_str}{level_str}{effort_str}{notes_str} to your watchlist.{configs_str}"

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
    # clear_wishlist
    # ------------------------------------------------------------------
    async def _clear_wishlist() -> str:
        """Remove ALL items from the watchlist at once. Use when the user
        says 'remove all', 'clear my list', or 'start fresh'."""
        count = await watchlist_repo.deactivate_all_for_user(discord_user_id)
        await prefs_repo.set(discord_user_id, "wishlist", "[]")
        if count == 0:
            return "Your watchlist is already empty."
        return f"Cleared {count} item(s) from your watchlist."

    # ------------------------------------------------------------------
    # update_wishlist_item
    # ------------------------------------------------------------------
    async def _update_wishlist_item(
        item_name: str,
        notification_level: str | None = None,
        max_price: float | None = None,
        notes: str | None = None,
        effort: str | None = None,
        search_configs: str | None = None,
    ) -> str:
        """Update an existing watchlist item's settings.

        Use this when the user wants to change settings on an item already on
        their watchlist — e.g. "only incredible deals" or "raise my budget to $200"
        or "not metal and old looking" or "add a search in Appleton"
        or "max effort on that one" or "go all out on TVs".

        Args:
            item_name: The item to update (must already be on the watchlist).
            notification_level: New notification level (all/good/great/incredible/free).
            max_price: New max price, or None to leave unchanged.
            notes: New preferences/notes for this item, or None to leave unchanged.
            effort: Evaluation thoroughness — "normal" or "max". None to leave unchanged.
                "max" = all images analyzed, best VLMs, no unbranded cap, full OCR.
            search_configs: New search configs as JSON string, or None to leave unchanged.
                Same format as add_to_wishlist.
        """
        items = await watchlist_repo.list_for_user(discord_user_id)
        matched = None
        for item in items:
            if item.interest.lower() == item_name.lower() and item.is_active:
                matched = item
                break

        if matched is None:
            return f"Item '{item_name}' not found on your watchlist. Can't update it."

        changes: list[str] = []
        if notification_level is not None:
            resolved = _normalize_notification_level(notification_level)
            if resolved is None:
                return f"Invalid notification level '{notification_level}'. Use: all, good, great, incredible, free."
            matched.notification_threshold = resolved
            changes.append(f"notifications → {resolved}")
        if max_price is not None:
            matched.max_price = max_price
            changes.append(f"max price → ${max_price}")
        if notes is not None:
            matched.notes = notes
            changes.append(f"notes → {notes}")
        if effort is not None:
            if effort in {"normal", "max"}:
                matched.effort = effort
                changes.append(f"effort → {effort}")
            else:
                return f"Invalid effort '{effort}'. Use: normal, max."
        if search_configs is not None:
            try:
                configs = json.loads(search_configs)
                matched.search_configs = configs
                changes.append(f"search configs → {len(configs)} config(s)")
            except (json.JSONDecodeError, TypeError):
                return "Invalid search_configs JSON."

        if not changes:
            return "Nothing to update. Specify notification_level, max_price, notes, or effort."

        await watchlist_repo.save(matched)

        # Also update in preferences JSON
        wishlist_json = await prefs_repo.get(discord_user_id, "wishlist")
        if wishlist_json:
            wishlist = json.loads(wishlist_json)
            for entry in wishlist:
                if entry["name"].lower() == item_name.lower():
                    if notification_level is not None:
                        entry["notification_level"] = notification_level.lower()
                    if max_price is not None:
                        entry["max_price"] = max_price
                    if notes is not None:
                        entry["notes"] = notes
                    if effort is not None:
                        entry["effort"] = effort
                    if search_configs is not None:
                        try:
                            entry["search_configs"] = json.loads(search_configs)
                        except (json.JSONDecodeError, TypeError):
                            pass
            await prefs_repo.set(discord_user_id, "wishlist", json.dumps(wishlist))

        return f"Updated '{item_name}': {', '.join(changes)}."

    # ------------------------------------------------------------------
    # show_wishlist
    # ------------------------------------------------------------------
    async def _show_wishlist() -> str:
        """Show all items currently on the wishlist.

        Reads from the watchlist database table (source of truth), not the
        preferences JSON cache.
        """
        items = await watchlist_repo.list_for_user(discord_user_id)
        active = [i for i in items if i.is_active]
        if not active:
            return "Your wishlist is empty."

        lines = []
        for item in active:
            line = f"- {item.interest}"
            if item.max_price is not None:
                line += f" (max ${item.max_price})"
            notif = item.notification_threshold or "good"
            line += f" -- notify: {notif}"
            if getattr(item, "effort", "normal") == "max":
                line += " -- [MAX EFFORT]"
            if item.notes:
                line += f" -- prefs: {item.notes}"
            if item.search_configs:
                cfg_parts = []
                for c in item.search_configs:
                    parts = []
                    if c.get("location"):
                        parts.append(c["location"])
                    if c.get("radius_miles"):
                        parts.append(f"{c['radius_miles']}mi")
                    if c.get("condition"):
                        parts.append(c["condition"].replace("used_", ""))
                    if c.get("max_price") is not None:
                        parts.append(f"max ${c['max_price']}")
                    if c.get("min_price") is not None:
                        parts.append(f"min ${c['min_price']}")
                    cfg_parts.append(" ".join(parts))
                line += f"\n  Searches: {' | '.join(cfg_parts)}"
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
    # add_to_exclusion
    # ------------------------------------------------------------------
    async def _add_to_exclusion(keyword: str) -> str:
        """Add a keyword to the exclusion list. Matching listings will be
        filtered out of general deal notifications.

        Args:
            keyword: Word or phrase to exclude (e.g. "mattress", "broken TV").
        """
        if exclusion_repo is None:
            return "Exclusion list feature is not available."

        existing = await exclusion_repo.list_for_user(discord_user_id)
        for item in existing:
            if item.keyword.lower() == keyword.lower():
                return f"'{keyword}' is already on your exclusion list."

        exc_item = ExclusionItem(
            keyword=keyword,
            discord_user_id=discord_user_id,
        )
        await exclusion_repo.save(exc_item)
        return f"Added '{keyword}' to your exclusion list. Listings matching this won't be shown."

    # ------------------------------------------------------------------
    # remove_from_exclusion
    # ------------------------------------------------------------------
    async def _remove_from_exclusion(keyword: str) -> str:
        """Remove a keyword from the exclusion list.

        Args:
            keyword: The keyword to remove.
        """
        if exclusion_repo is None:
            return "Exclusion list feature is not available."

        deleted = await exclusion_repo.delete_for_user(keyword, discord_user_id)
        if deleted:
            return f"Removed '{keyword}' from your exclusion list."
        return f"'{keyword}' was not found on your exclusion list."

    # ------------------------------------------------------------------
    # show_exclusion_list
    # ------------------------------------------------------------------
    async def _show_exclusion_list() -> str:
        """Show all keywords on the exclusion list."""
        if exclusion_repo is None:
            return "Exclusion list feature is not available."

        items = await exclusion_repo.list_for_user(discord_user_id)
        if not items:
            return "Your exclusion list is empty."

        lines = [f"- {item.keyword}" for item in items]
        return "Your exclusion list:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Assemble and return
    # ------------------------------------------------------------------
    tools = [
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
            description="Remove a single interest from the watchlist.",
        ),
        StructuredTool.from_function(
            coroutine=_clear_wishlist,
            name="clear_wishlist",
            description=(
                "Remove ALL items from the watchlist at once. Use when the user"
                " wants to clear everything or start fresh."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_update_wishlist_item,
            name="update_wishlist_item",
            description=(
                "Update an existing watchlist item's notification level or max price."
                " Use when the user wants to change settings on an item already on"
                " their watchlist."
            ),
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

    # Exclusion tools (only if repo wired up)
    if exclusion_repo is not None:
        tools.extend([
            StructuredTool.from_function(
                coroutine=_add_to_exclusion,
                name="add_to_exclusion",
                description=(
                    "Add a keyword to the exclusion list. Listings matching excluded"
                    " keywords will be filtered out of deal notifications."
                ),
            ),
            StructuredTool.from_function(
                coroutine=_remove_from_exclusion,
                name="remove_from_exclusion",
                description="Remove a keyword from the exclusion list.",
            ),
            StructuredTool.from_function(
                coroutine=_show_exclusion_list,
                name="show_exclusion_list",
                description="Show all keywords the user has excluded from notifications.",
            ),
        ])

    return tools
