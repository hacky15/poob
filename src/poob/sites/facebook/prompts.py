"""LLM navigation prompts for Facebook Marketplace.

These prompts are the most frequently-tuned part of the system.
Isolated here so they can iterate independently from adapter logic.
"""

from __future__ import annotations

def build_search_prompt(
    keywords: str,
    max_price: float | None = None,
    location: str | None = None,
    email: str | None = None,
    password: str | None = None,
    max_listings: int = 20,
    category: str | None = None,
) -> str:
    """Build a browser-use agent task prompt for searching Facebook Marketplace.

    Args:
        keywords: Search terms (e.g. "PS5", "mountain bike").
        max_price: Maximum price filter, or None to skip.
        location: Location filter string, or None to skip.
        email: Facebook login email, or None to skip login.
        password: Facebook login password, or None to skip login.
        max_listings: Maximum number of listings to extract.
        category: Item category (e.g. "furniture") to narrow results.

    Returns:
        A complete task prompt string for the browser-use agent.
    """
    # Always use general /search/ to cast a wide net — many sellers skip
    # categorization. Relevance filtering happens post-scrape instead.
    search_url = (
        f"https://www.facebook.com/marketplace/search/"
        f"?query={keywords.replace(' ', '+')}"
        f"&sortBy=creation_time_descend"
    )
    if max_price is not None:
        search_url += f"&maxPrice={max_price:.0f}"

    login_instruction = ""
    if email and password:
        login_instruction = f"""STEP 1 — LOGIN (if required):
Navigate to {search_url}

If you see a login page with email and password fields:
1. Type "{email}" into the email/phone input field.
2. Type "{password}" into the password input field.
3. IMPORTANT: Find and click the button labeled "Log In" that SUBMITS the form.
   - Do NOT click "Show password" or "Hide password" — those are toggle icons, not the login button.
   - The login button is typically a large blue button below the password field.
   - If you cannot find a clickable "Log In" button, press Enter in the password field to submit.
4. Wait for the page to load after login.

If you see a dismissable popup or overlay (like "Log in to continue"), click the Close/X button to dismiss it.

"""

    location_instruction = ""
    if location is not None:
        location_instruction = (
            f"\n- Set the location to \"{location}\" if a location filter is available."
        )

    return f"""{login_instruction}STEP 2 — NAVIGATE:
Navigate to {search_url}
This URL already contains the search query and price filter. Wait for results to load.{location_instruction}

STEP 3 — EXTRACT DATA:
Use the "extract" action with extract_links=True to extract the first {max_listings} listings.
For each listing extract: title, price, location.
Return as a JSON array. Set start_from_char=0.

STEP 4 — GET LISTING URLs:
Run this JavaScript with the "evaluate" action:
Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]')).slice(0, {max_listings}).map(el => el.href.split('?')[0])

STEP 5 — RETURN RESULTS:
Call "done" with ALL data. Combine the extracted listings JSON and the URL array.
Keep it compact — do NOT expand URLs or add extra text. Return raw data."""


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
