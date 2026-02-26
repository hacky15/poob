"""Parse browser-use agent output into Listing dataclass instances."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.facebook.parser")


def _extract_json(text: str) -> str | None:
    """Extract JSON from text, handling markdown code fences.

    Args:
        text: Raw agent output that may contain JSON in code fences.

    Returns:
        The extracted JSON string, or None if no JSON found.
    """
    # Try markdown code fence first: ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Try to find a JSON array or object directly
    # Look for [ ... ] or { ... }
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        if start == -1:
            continue
        # Find the matching closing bracket
        end = text.rfind(end_char)
        if end > start:
            return text[start : end + 1]

    return None


def parse_listings(raw_output: str, site: str) -> list[Listing]:
    """Parse agent output into a list of Listing objects.

    Handles various output formats: raw JSON arrays, markdown-fenced JSON,
    and partial/malformed data. Always returns a list (empty on failure).

    Args:
        raw_output: Raw text output from the browser-use agent.
        site: Site identifier to set on each listing (e.g. 'facebook_marketplace').

    Returns:
        List of Listing objects. Empty list if parsing fails.
    """
    if not raw_output or not raw_output.strip():
        return []

    json_str = _extract_json(raw_output)
    if json_str is None:
        log.warning("No JSON found in agent output", output_preview=raw_output[:200])
        return []

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        log.warning("Failed to parse JSON from agent output", error=str(exc))
        return []

    # Normalize: if it's a single object, wrap in a list
    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        log.warning("Expected JSON array, got", type=type(data).__name__)
        return []

    listings: list[Listing] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            listing = _dict_to_listing(item, site)
            listings.append(listing)
        except Exception as exc:
            log.warning("Failed to parse listing item", error=str(exc), item=item)

    log.info("Parsed listings from agent output", count=len(listings))
    return listings


def _dict_to_listing(data: dict, site: str) -> Listing:
    """Convert a dictionary to a Listing dataclass.

    Handles field name variations (image_url vs image_urls) and
    missing optional fields gracefully.

    Args:
        data: Dictionary with listing fields.
        site: Site identifier.

    Returns:
        A Listing instance.
    """
    # Handle image_url -> image_urls conversion
    image_urls: list[str] = []
    if "image_urls" in data and isinstance(data["image_urls"], list):
        image_urls = data["image_urls"]
    elif "image_url" in data and data["image_url"]:
        image_urls = [data["image_url"]]

    return Listing(
        site=site,
        external_id=str(data.get("external_id", "")),
        title=data.get("title", ""),
        price=_parse_price(data.get("price")),
        description=data.get("description", ""),
        location=data.get("location", ""),
        seller_name=data.get("seller_name", ""),
        image_urls=image_urls,
        listing_url=data.get("listing_url", ""),
        scraped_at=datetime.now(timezone.utc),
        raw_data=data,
    )


def _parse_price(value: object) -> float | None:
    """Parse a price value that may be a float, int, or string.

    Args:
        value: Raw price value from parsed JSON.

    Returns:
        Float price or None if unparseable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Strip currency symbols and commas
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None
