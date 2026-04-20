"""Parse browser-use agent output into Listing dataclass instances."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from poob.storage.models import Listing
from poob.utils.logging import get_logger

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


def _parse_json_robust(json_str: str) -> list[dict] | None:
    """Try multiple strategies to parse JSON that may be corrupted by an LLM.

    The browser-use agent's qwen model often corrupts JSON at the tail end of
    long outputs: backticks instead of quotes, Unicode garbage, repeated URLs.
    This function tries progressively more aggressive repair strategies.

    Args:
        json_str: Raw JSON string, possibly corrupted.

    Returns:
        List of dicts, or None if all strategies fail.
    """
    # Strategy 1: Parse as-is
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass

    # Strategy 2: Fix common LLM corruption (backticks → quotes, strip garbage)
    fixed = json_str
    fixed = fixed.replace("``", '"')  # backtick pairs → double quote
    fixed = re.sub(r"[^\x20-\x7E\n\r\t]", "", fixed)  # strip non-ASCII garbage
    try:
        data = json.loads(fixed)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass

    # Strategy 3: Extract individual JSON objects via regex
    # Find all { ... } blocks that look like listing objects
    objects: list[dict] = []
    for match in re.finditer(r"\{[^{}]{20,800}\}", fixed):
        candidate = match.group()
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and ("title" in obj or "price" in obj):
                objects.append(obj)
        except json.JSONDecodeError:
            continue

    if objects:
        return objects

    return None


def parse_listings(raw_output: str, site: str) -> list[Listing]:
    """Parse agent output into a list of Listing objects.

    Handles various output formats: raw JSON arrays, markdown-fenced JSON,
    numbered text lists, and CSV/dash-separated lists. URLs found anywhere
    in the output are merged into listings that lack them.

    Args:
        raw_output: Raw text output from the browser-use agent.
        site: Site identifier to set on each listing (e.g. 'facebook_marketplace').

    Returns:
        List of Listing objects. Empty list if parsing fails.
    """
    if not raw_output or not raw_output.strip():
        return []

    data: list[dict] | None = None

    # Try JSON first, then fall back to plain-text formats
    json_str = _extract_json(raw_output)
    if json_str is not None:
        data = _parse_json_robust(json_str)

    # Try plain-text numbered list format
    if data is None:
        data = _parse_text_listings(raw_output)

    # Try CSV/dash-separated format: "- Title, $Price, Location"
    if data is None:
        data = _parse_csv_listings(raw_output)

    if data is None:
        log.warning("No parseable data in agent output", output_preview=raw_output[:200])
        return []

    # Merge marketplace URLs found elsewhere in the output into listings that lack them
    _merge_marketplace_urls(data, raw_output)

    return _data_to_listings(data, site)


def _merge_marketplace_urls(data: list[dict], raw_output: str) -> None:
    """Merge marketplace item URLs found in raw output into listings that lack them.

    The browser-use agent may return listing data and URLs separately (e.g. from
    extract + find_elements). This function finds all marketplace item URLs in the
    raw output and assigns them to listings by position.

    Args:
        data: List of listing dicts (modified in place).
        raw_output: Full raw agent output to scan for URLs.
    """
    # Find all unique marketplace item URLs in the output
    all_urls = re.findall(
        r"https?://(?:www\.)?facebook\.com/marketplace/item/\d+[^\s\]\)\"',]*",
        raw_output,
    )
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in all_urls:
        # Normalize: strip trailing punctuation and query params for dedup
        clean = url.split("?")[0].rstrip("/")
        if clean not in seen:
            seen.add(clean)
            unique_urls.append(url.split("?")[0])  # keep clean URL without query params

    if not unique_urls:
        return

    # Assign URLs to listings that don't already have one
    url_idx = 0
    for item in data:
        if url_idx >= len(unique_urls):
            break
        existing_url = item.get("listing_url", "")
        if not existing_url or "/marketplace/item/" not in existing_url:
            item["listing_url"] = unique_urls[url_idx]
            # Extract external_id from URL
            id_match = re.search(r"/item/(\d+)", unique_urls[url_idx])
            if id_match:
                item["external_id"] = id_match.group(1)
            url_idx += 1


def _data_to_listings(data: list[dict], site: str) -> list[Listing]:
    """Convert a list of dicts to Listing objects, skipping invalid entries."""
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


def _parse_text_listings(text: str) -> list[dict] | None:
    """Parse plain-text numbered listing format from browser-use extract action.

    Handles output like:
        1. Title: Black Coffee Table
           Price: $60
           Location: Green Bay, WI
           Listing_url: /marketplace/item/123456/...

    Args:
        text: Raw text output.

    Returns:
        List of dicts, or None if format doesn't match.
    """
    # Split into blocks by numbered items (1. ... 2. ... etc.)
    # Prepend newline so the first "1." is also captured by the split pattern
    blocks = re.split(r"(?:^|\n)\s*\d+\.\s+", "\n" + text)
    if len(blocks) < 2:  # First element is empty/preamble, rest are items
        return None

    results: list[dict] = []
    for block in blocks[1:]:  # Skip preamble before first "1."
        item: dict = {}
        for line in block.strip().splitlines():
            line = line.strip()
            # Match "Key: Value" patterns (case-insensitive)
            match = re.match(r"(\w[\w_]*)\s*:\s*(.+)", line)
            if match:
                key = match.group(1).lower().strip()
                value = match.group(2).strip()
                item[key] = value

        if not item:
            continue

        # Normalize field names
        listing: dict = {}
        listing["title"] = item.get("title", "")
        listing["price"] = item.get("price", "")
        listing["location"] = item.get("location", "")

        # Handle listing_url: convert relative to absolute
        url = item.get("listing_url", item.get("url", item.get("link", "")))
        if url and not url.startswith("http"):
            # Extract clean path before query params
            clean_url = url.split("?")[0]
            url = "https://www.facebook.com" + clean_url
        listing["listing_url"] = url

        # Extract external_id from URL
        id_match = re.search(r"/item/(\d+)", url)
        listing["external_id"] = id_match.group(1) if id_match else ""

        listing["image_url"] = item.get("image_url", "")
        listing["seller_name"] = item.get("seller_name", "")

        if listing["title"] or listing["price"]:
            results.append(listing)

    return results if results else None


def _parse_csv_listings(text: str) -> list[dict] | None:
    """Parse dash-separated CSV format from browser-use extract action.

    Handles output like:
        - Black Coffee Table, $60, Green Bay, WI
        - Coffee Table, $45–$50, Green Bay, WI
        - Wooden Table, $100, Appleton, WI [https://www.facebook.com/marketplace/item/123]

    The price (anchored by $) splits title from location. An optional URL
    in brackets or parentheses at the end is captured if present.

    Args:
        text: Raw text output.

    Returns:
        List of dicts, or None if format doesn't match.
    """
    results: list[dict] = []

    for line in text.strip().splitlines():
        line = line.strip()
        # Must start with "- " or "* " (bullet list)
        if not re.match(r"^[-*]\s+", line):
            continue
        line = re.sub(r"^[-*]\s+", "", line)

        # Extract trailing URL in brackets [url] or parentheses (url)
        url = ""
        url_match = re.search(r"[\[\(](https?://[^\]\)]+)[\]\)]$", line)
        if url_match:
            url = url_match.group(1)
            line = line[: url_match.start()].strip().rstrip(",")

        # Also check for bare URL at end (no brackets)
        if not url:
            bare_url = re.search(r"(https?://\S+)$", line)
            if bare_url:
                url = bare_url.group(1)
                line = line[: bare_url.start()].strip().rstrip(",")

        # Find price anchor: $digits (possibly with –/- range like $45–$50)
        price_match = re.search(
            r"\$[\d,]+(?:\.\d+)?(?:\s*[\u2013\-]\s*\$?[\d,]+(?:\.\d+)?)?", line,
        )
        if not price_match:
            # No price found — treat whole line as title
            if line.strip():
                results.append({"title": line.strip(), "price": "", "location": ""})
            continue

        title = line[: price_match.start()].strip().rstrip(",").strip()
        price_str = price_match.group(0).strip().rstrip(",")
        location = line[price_match.end() :].strip().lstrip(",").strip()

        listing: dict = {
            "title": title,
            "price": price_str,
            "location": location,
            "listing_url": url,
        }

        # Extract external_id from URL if present
        if url:
            id_match = re.search(r"/item/(\d+)", url)
            listing["external_id"] = id_match.group(1) if id_match else ""

        if title or price_str:
            results.append(listing)

    return results if results else None


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

    title = data.get("title", "")
    price = _parse_price(data.get("price"))

    # Rescue embedded price from title when JS extractor missed it
    # Handles concatenated text like "Just listed$200Haaka sausage stuffer"
    if price is None and title:
        embedded = re.search(r"\$(\d[\d,]*\.?\d{0,2})", title)
        if embedded:
            try:
                price = float(embedded.group(1).replace(",", ""))
                # Strip the price and common FB prefixes from title
                title = re.sub(
                    r"(?:Just listed|Listed \w+ ago)?\s*\$\d[\d,]*\.?\d{0,2}\s*",
                    "",
                    title,
                ).strip()
            except ValueError:
                pass

    # Parse freshness text into posted_at when available
    posted_at = _parse_freshness(data.get("freshness", ""))

    return Listing(
        site=site,
        external_id=str(data.get("external_id", "")),
        title=title,
        price=price,
        description=data.get("description", ""),
        location=data.get("location", ""),
        seller_name=data.get("seller_name", ""),
        image_urls=image_urls,
        listing_url=data.get("listing_url", ""),
        posted_at=posted_at,
        scraped_at=datetime.now(timezone.utc),
        raw_data=data,
        is_sponsored=bool(data.get("is_sponsored", False)),
    )


def _parse_freshness(text: str) -> datetime | None:
    """Parse Facebook freshness text into a datetime estimate.

    Args:
        text: Freshness string like "Just listed", "Listed 3 hours ago", etc.

    Returns:
        Estimated posted_at datetime, or None if unparseable.
    """
    if not text:
        return None

    text = text.strip().lower()
    now = datetime.now(timezone.utc)

    if "just listed" in text:
        return now

    # "Listed X minutes ago"
    m = re.search(r"(\d+)\s*minutes?\s*ago", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    # "Listed X hours ago"
    m = re.search(r"(\d+)\s*hours?\s*ago", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    # "Listed yesterday"
    if "yesterday" in text:
        return now - timedelta(hours=24)

    # "Listed X days ago"
    m = re.search(r"(\d+)\s*days?\s*ago", text)
    if m:
        return now - timedelta(days=int(m.group(1)))

    # "Listed X weeks ago"
    m = re.search(r"(\d+)\s*weeks?\s*ago", text)
    if m:
        return now - timedelta(weeks=int(m.group(1)))

    return None


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
        # Handle "$45$50" format (current price + original price) — take first
        prices = re.findall(r"\$?([\d,]+\.?\d*)", value)
        if prices:
            try:
                return float(prices[0].replace(",", ""))
            except ValueError:
                pass
        # Fallback: strip currency symbols and commas
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None
