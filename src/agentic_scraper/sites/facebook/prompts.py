"""LLM navigation prompts for Facebook Marketplace.

These prompts are the most frequently-tuned part of the system.
Isolated here so they can iterate independently from adapter logic.
"""

from __future__ import annotations


def build_search_prompt(
    keywords: str,
    max_price: float | None = None,
    location: str | None = None,
) -> str:
    """Build a browser-use agent task prompt for searching Facebook Marketplace.

    Args:
        keywords: Search terms (e.g. "PS5", "mountain bike").
        max_price: Maximum price filter, or None to skip.
        location: Location filter string, or None to skip.

    Returns:
        A complete task prompt string for the browser-use agent.
    """
    price_instruction = ""
    if max_price is not None:
        price_instruction = (
            f"\n- Set the maximum price filter to ${max_price:.0f}."
        )

    location_instruction = ""
    if location is not None:
        location_instruction = (
            f"\n- Set the location to \"{location}\" if a location filter is available."
        )

    return f"""Go to Facebook Marketplace at https://www.facebook.com/marketplace.

Search for "{keywords}" in the search bar.{price_instruction}{location_instruction}

Scroll through the search results slowly. For each listing visible, extract:
- title: The listing title
- price: The numeric price (as a float, e.g. 250.0)
- location: The seller's location
- seller_name: The seller's name (if visible)
- listing_url: The URL to the listing page
- image_url: The main image URL
- external_id: The listing ID from the URL (the numeric part)

Return the results as a JSON array. Example format:
[
    {{
        "title": "PlayStation 5",
        "price": 250.0,
        "location": "Portland, OR",
        "seller_name": "John D.",
        "listing_url": "https://facebook.com/marketplace/item/12345",
        "image_url": "https://scontent.xx.fbcdn.net/...",
        "external_id": "12345"
    }}
]

Extract up to 20 listings. Return ONLY the JSON array, no additional text."""


DETAIL_PROMPT = """Navigate to this Facebook Marketplace listing: {listing_url}

Extract all available details:
- title: The full listing title
- price: The numeric price as a float
- description: The full item description
- location: The seller's location
- seller_name: The seller's name
- image_urls: List of all image URLs for the listing
- posted_at: When the listing was posted (if visible)
- external_id: The listing ID from the URL

Return the result as a single JSON object (not an array). Example:
{{
    "title": "PlayStation 5 Disc Edition",
    "price": 250.0,
    "description": "Like new, barely used...",
    "location": "Portland, OR",
    "seller_name": "John D.",
    "image_urls": ["https://...jpg", "https://...jpg"],
    "posted_at": "2 hours ago",
    "external_id": "12345"
}}

Return ONLY the JSON object, no additional text."""
