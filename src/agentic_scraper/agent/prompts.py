"""System prompt builder for the conversational agent."""

from __future__ import annotations

import json


def build_system_prompt(
    *,
    wishlist: list[dict] | None = None,
    location: dict | None = None,
    search_priorities: dict | None = None,
) -> str:
    """Build the dynamic system prompt with user preferences injected.

    Args:
        wishlist: List of wishlist item dicts with name, max_price, priority.
        location: Location dict with city, radius_miles.
        search_priorities: Search priority dict with just_listed_first, etc.

    Returns:
        Complete system prompt string.
    """
    sections = [_BASE_PROMPT]

    sections.append(_wishlist_section(wishlist))
    sections.append(_location_section(location))
    sections.append(_priorities_section(search_priorities))
    sections.append(_INSTRUCTIONS)

    return "\n\n".join(sections)


_BASE_PROMPT = """\
You are a deal-hunting assistant. You continuously patrol Facebook Marketplace \
and other online marketplaces to find great deals for the user.

The system automatically patrols ALL new listings in the Appleton area, checking \
every few minutes during peak hours. You have tools to manage the user's interest \
list (things to look out for), trigger immediate patrols, and view recent deals.

CRITICAL RULE: You MUST call the appropriate tool for ANY action request. \
NEVER describe what you would do — actually do it by calling the tool. \
For example, if the user says "scan" or "patrol", you MUST call the \
trigger_scan tool. If they want to add an interest, MUST call add_to_wishlist. \
Do NOT respond with text like "I'll start a patrol" without actually calling \
the tool."""


def _wishlist_section(wishlist: list[dict] | None) -> str:
    if not wishlist:
        return "## Current Wishlist\nNo items yet."

    lines = ["## Current Wishlist"]
    for item in wishlist:
        line = f"- {item['name']}"
        if "max_price" in item and item["max_price"] is not None:
            line += f" (max ${item['max_price']})"
        prio = item.get("priority", "normal")
        if prio != "normal":
            line += f" [{prio}]"
        lines.append(line)
    return "\n".join(lines)


def _location_section(location: dict | None) -> str:
    if not location:
        return "## Location\nNot set."

    city = location.get("city", "Unknown")
    radius = location.get("radius_miles")
    if radius:
        return f"## Location\n{city} ({radius} mile radius)"
    return f"## Location\n{city}"


def _priorities_section(search_priorities: dict | None) -> str:
    if not search_priorities:
        return "## Search Priorities\nDefault settings."

    lines = ["## Search Priorities"]
    if search_priorities.get("just_listed_first"):
        lines.append("- Prioritize just-listed items: yes")
    if search_priorities.get("desperate_seller_detection"):
        lines.append(
            "- Detect desperate sellers (\"must sell\", \"need gone\", \"make offer\"): yes"
        )
    if "min_deal_score" in search_priorities:
        lines.append(f"- Minimum deal score: {search_priorities['min_deal_score']}")
    if len(lines) == 1:
        lines.append("Default settings.")
    return "\n".join(lines)


_INSTRUCTIONS = """\
## Instructions
- ALWAYS call a tool when the user requests an action. Never just describe the action.
- Be concise and helpful.
- When adding interests, optionally specify a category (e.g. "furniture", \
"electronics", "clothing", "toys", "appliances", "tools", "vehicles"). \
Infer the category from context (a coffee table is furniture, a PS5 is electronics).
- When adding or removing interests, call the tool first, then confirm what you did.
- If the user is vague, ask a clarifying question rather than guessing.

## Tool Selection Guide
- User wants to scan/patrol/check marketplace → trigger_scan
- User wants to pause scanning → pause_patrol
- User wants to resume scanning → resume_patrol
- User asks about patrol status or "is it running?" → get_scanner_status
- User asks to see deals, "any deals?", "what's new?" → get_recent_deals
- User asks for details about a deal, "tell me more", "why is that a deal?" → get_deal_details
- User wants to browse/search listings, "show me X under $Y" → search_listings
- User asks about their interests/watchlist → show_wishlist
- User asks about their settings/location/preferences → get_preferences
- User asks about scan history/stats, "how many deals today?" → get_scan_history
- User wants to change location/radius/settings → update_preferences
- User asks "what can you do?" → describe your capabilities (no tool needed)"""
