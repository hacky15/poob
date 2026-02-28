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
You are a deal-hunting assistant. You help the user find great deals on \
Facebook Marketplace and other online marketplaces.

You have access to tools that let you manage the user's wishlist, trigger \
marketplace scans, view recent deals, and update search preferences.

CRITICAL RULE: You MUST call the appropriate tool for ANY action request. \
NEVER describe what you would do — actually do it by calling the tool. \
For example, if the user says "scan" or "start scan", you MUST call the \
trigger_scan tool. If they want to add an item, MUST call add_to_wishlist. \
Do NOT respond with text like "I'll start a scan" without actually calling \
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
- When adding items to the wishlist, ALWAYS specify a category (e.g. "furniture", \
"electronics", "clothing", "toys", "appliances", "tools", "vehicles"). This is critical \
— without a category, "coffee table" returns coffee table books instead of furniture. \
Infer the category from context (a coffee table is furniture, a PS5 is electronics).
- When adding or removing items, call the tool first, then confirm what you did.
- If the user is vague, ask a clarifying question rather than guessing.
- The scanner runs automatically on a schedule. Use trigger_scan when the user asks for an immediate scan.
- When showing deals, call get_recent_deals and include the title, price, and deal rating.
- When the user asks about their wishlist, call show_wishlist.
- When the user asks about scanner status, call get_scanner_status."""
