"""Discord embed builders for deals, listings, watchlists, and status."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from poob.skills.models import DealProvenance
from poob.storage.models import Deal, DealScore, Listing, WatchItem
from poob.utils.content import clean_fb_description

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


def _format_provenance_field(deal: Deal) -> str | None:
    """Build compact provenance summary for the Discord embed.

    Returns None if no provenance data is available (old deals).
    """
    if not deal.provenance_json:
        return None

    prov = DealProvenance.from_json(deal.provenance_json)
    lines: list[str] = []

    # Item identified by VLM
    if prov.vlm_item_identified:
        lines.append(f"**Item:** {prov.vlm_item_identified}")

    # Condition
    if prov.vlm_condition and prov.vlm_condition != "unknown":
        cond_str = prov.vlm_condition.capitalize()
        if prov.vlm_condition_notes:
            cond_str += f" ({prov.vlm_condition_notes[:60]})"
        lines.append(f"**Condition:** {cond_str}")

    # VLM providers
    if prov.vlm_providers:
        prov_str = ", ".join(prov.vlm_providers)
        if prov.vlm_agreement is not None:
            prov_str += f" (agreement: {prov.vlm_agreement:.2f})"
        lines.append(f"**Evaluated by:** {prov_str}")

    # Price source
    if prov.price_source:
        price_str = prov.price_source
        if prov.price_sample_count:
            price_str += f" ({prov.price_sample_count} samples"
            if prov.price_confidence:
                price_str += f", conf: {prov.price_confidence:.1f}"
            price_str += ")"
        lines.append(f"**Price data:** {price_str}")

    # Score adjustments
    if prov.score_adjustments:
        lines.append(f"**Adjusted:** {'; '.join(prov.score_adjustments)}")

    # Scam signals
    if prov.scam_signals:
        lines.append(f"**Scam signals:** {', '.join(prov.scam_signals)}")

    return "\n".join(lines) if lines else None


def format_deal_embed(
    deal: Deal, listing: Listing, watch_interest: str = ""
) -> discord.Embed:
    """Build a Discord embed for a deal alert.

    Args:
        deal: The deal to display.
        listing: The associated listing.
        watch_interest: Optional watchlist interest that matched (shown in DMs).

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

    if watch_interest:
        embed.add_field(name="Matched Interest", value=watch_interest, inline=True)

    # Seller's description — strip FB UI chrome, truncate to Discord limit
    desc = clean_fb_description(listing.description)
    if desc:
        if len(desc) > 300:
            desc = desc[:297] + "..."
        embed.add_field(name="Seller Description", value=desc, inline=False)

    if deal.llm_reasoning:
        embed.add_field(name="Why it's a deal", value=deal.llm_reasoning[:1024], inline=False)

    # Provenance: show evaluation details when available
    provenance_text = _format_provenance_field(deal)
    if provenance_text:
        embed.add_field(
            name="Evaluation Details",
            value=provenance_text[:1024],
            inline=False,
        )

    if listing.image_urls:
        embed.set_image(url=listing.image_urls[0])

    embed.set_footer(text="React: \u2705 Claimed | \u274c Overpriced | \U0001f6ab Scam | \U0001f610 Not for me")

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
            title="Your Interests",
            description="No active interests. Use `!watch <interest>` to add one.",
            color=discord.Colour.light_grey(),
        )

    embed = discord.Embed(
        title="Your Interests",
        color=discord.Colour.blue(),
    )

    for item in watch_items:
        price_str = f" (max ${item.max_price:.0f})" if item.max_price else ""
        location_str = f" near {item.location}" if item.location else ""
        status = "Active" if item.is_active else "Paused"
        threshold = item.notification_threshold or "good"
        threshold_str = f" | Notify: {threshold}" if threshold != "good" else ""
        effort = getattr(item, "effort", "normal")
        effort_str = " | **MAX EFFORT**" if effort == "max" else ""

        embed.add_field(
            name=f"{item.interest}{price_str}",
            value=f"ID: `{item.id}`{location_str} | {status}{threshold_str}{effort_str}",
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
    display_timezone: str = "America/Chicago",
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

    embed = discord.Embed(title="Patrol Status", color=color)
    embed.add_field(name="Status", value=status_str, inline=True)

    tz = ZoneInfo(display_timezone)
    tz_abbrev = "CT"  # Central Time
    if last_scan_time:
        last_local = last_scan_time.astimezone(tz)
        last_str = last_local.strftime(f"%Y-%m-%d %H:%M {tz_abbrev}")
    else:
        last_str = "Never"
    embed.add_field(name="Last Patrol", value=last_str, inline=True)

    if next_scan_time:
        next_local = next_scan_time.astimezone(tz)
        next_str = next_local.strftime(f"%Y-%m-%d %H:%M {tz_abbrev}")
    else:
        next_str = "N/A"
    embed.add_field(name="Next Patrol", value=next_str, inline=True)

    embed.add_field(
        name="Sites",
        value=", ".join(registered_sites) if registered_sites else "None",
        inline=False,
    )

    return embed
