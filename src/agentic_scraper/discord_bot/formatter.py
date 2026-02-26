"""Discord embed builders for deals, listings, watchlists, and status."""

from __future__ import annotations

from datetime import datetime

import discord

from agentic_scraper.storage.models import Deal, DealScore, Listing, WatchItem

# Color coding by deal score
SCORE_COLORS: dict[DealScore, discord.Colour] = {
    DealScore.UNKNOWN: discord.Colour.light_grey(),
    DealScore.FAIR: discord.Colour.light_grey(),
    DealScore.GOOD: discord.Colour.green(),
    DealScore.GREAT: discord.Colour.gold(),
    DealScore.INCREDIBLE: discord.Colour.red(),
}

SCORE_EMOJI: dict[DealScore, str] = {
    DealScore.UNKNOWN: "",
    DealScore.FAIR: "",
    DealScore.GOOD: "[!]",
    DealScore.GREAT: "[!!]",
    DealScore.INCREDIBLE: "[!!!]",
}


def format_deal_embed(deal: Deal, listing: Listing) -> discord.Embed:
    """Build a Discord embed for a deal alert.

    Args:
        deal: The deal to display.
        listing: The associated listing.

    Returns:
        A formatted Discord Embed.
    """
    emoji = SCORE_EMOJI.get(deal.score, "")
    title = f"{emoji} {listing.title}".strip()
    color = SCORE_COLORS.get(deal.score, discord.Colour.light_grey())

    embed = discord.Embed(
        title=title,
        url=listing.listing_url or None,
        color=color,
    )

    embed.add_field(name="Price", value=f"${listing.price:.2f}" if listing.price else "N/A", inline=True)

    if deal.estimated_market_price:
        embed.add_field(
            name="Est. Market",
            value=f"${deal.estimated_market_price:.2f}",
            inline=True,
        )

    if deal.discount_pct:
        embed.add_field(name="Discount", value=f"{deal.discount_pct:.0f}% off", inline=True)

    embed.add_field(name="Location", value=listing.location or "Unknown", inline=True)
    embed.add_field(name="Site", value=listing.site, inline=True)
    embed.add_field(name="Deal Score", value=deal.score.value.upper(), inline=True)

    if deal.llm_reasoning:
        embed.add_field(name="Why it's a deal", value=deal.llm_reasoning, inline=False)

    if listing.image_urls:
        embed.set_image(url=listing.image_urls[0])

    return embed


def format_listing_embed(listing: Listing) -> discord.Embed:
    """Build a Discord embed for a single listing.

    Args:
        listing: The listing to display.

    Returns:
        A formatted Discord Embed.
    """
    embed = discord.Embed(
        title=listing.title,
        url=listing.listing_url or None,
        color=discord.Colour.blue(),
    )

    embed.add_field(name="Price", value=f"${listing.price:.2f}" if listing.price else "N/A", inline=True)
    embed.add_field(name="Location", value=listing.location or "Unknown", inline=True)
    embed.add_field(name="Site", value=listing.site, inline=True)

    if listing.seller_name:
        embed.add_field(name="Seller", value=listing.seller_name, inline=True)

    if listing.image_urls:
        embed.set_image(url=listing.image_urls[0])

    return embed


def format_watchlist_embed(watch_items: list[WatchItem]) -> discord.Embed:
    """Build a Discord embed showing a user's watch list.

    Args:
        watch_items: The user's watch items.

    Returns:
        A formatted Discord Embed.
    """
    if not watch_items:
        return discord.Embed(
            title="Your Watchlist",
            description="No active watches. Use `!watch <keywords>` to add one.",
            color=discord.Colour.light_grey(),
        )

    embed = discord.Embed(
        title="Your Watchlist",
        color=discord.Colour.blue(),
    )

    for item in watch_items:
        price_str = f" (max ${item.max_price:.0f})" if item.max_price else ""
        location_str = f" near {item.location}" if item.location else ""
        status = "Active" if item.is_active else "Paused"

        embed.add_field(
            name=f"{item.keywords}{price_str}",
            value=f"ID: `{item.id}`{location_str} | {status}",
            inline=False,
        )

    return embed


def format_status_embed(
    *,
    is_running: bool,
    is_paused: bool,
    last_scan_time: datetime | None,
    next_scan_time: datetime | None,
    registered_sites: list[str],
) -> discord.Embed:
    """Build a Discord embed showing scanner status.

    Args:
        is_running: Whether the scheduler is active.
        is_paused: Whether scanning is paused.
        last_scan_time: When the last scan completed.
        next_scan_time: When the next scan is estimated.
        registered_sites: Names of registered site adapters.

    Returns:
        A formatted Discord Embed.
    """
    if is_paused:
        status_str = "Paused"
        color = discord.Colour.orange()
    elif is_running:
        status_str = "Running"
        color = discord.Colour.green()
    else:
        status_str = "Stopped"
        color = discord.Colour.red()

    embed = discord.Embed(title="Scanner Status", color=color)
    embed.add_field(name="Status", value=status_str, inline=True)

    last_str = last_scan_time.strftime("%Y-%m-%d %H:%M UTC") if last_scan_time else "Never"
    embed.add_field(name="Last Scan", value=last_str, inline=True)

    next_str = next_scan_time.strftime("%Y-%m-%d %H:%M UTC") if next_scan_time else "N/A"
    embed.add_field(name="Next Scan", value=next_str, inline=True)

    embed.add_field(
        name="Sites",
        value=", ".join(registered_sites) if registered_sites else "None",
        inline=False,
    )

    return embed
