"""LLM prompts for structured data extraction from Facebook Marketplace.

Used when JavaScript DOM extraction fails or returns insufficient data.
These prompts receive raw page text (markdown) and return structured JSON.
Separate from prompts.py (which contains browser-use agent navigation prompts).
"""

EXTRACT_LISTINGS_PROMPT = """\
You are extracting Facebook Marketplace listings from a web page.
The page content below is a clean markdown representation of the page.

Extract ALL visible marketplace listings. For each listing, provide:
- title: The listing title
- price: Numeric price as a float (e.g., 250.0). Use 0 for "Free" items.
- location: Seller location (city, state)
- listing_url: Full URL containing /marketplace/item/ (if visible)
- external_id: The numeric ID from the URL (digits after /item/)
- image_url: First image URL if available, empty string otherwise

Return a JSON object with a "listings" key containing an array.
If no listings are found, return {{"listings": []}}.

Example output:
{{"listings": [
  {{
    "title": "PlayStation 5",
    "price": 250.0,
    "location": "Portland, OR",
    "listing_url": "https://www.facebook.com/marketplace/item/12345",
    "external_id": "12345",
    "image_url": ""
  }}
]}}

Page content:
{page_content}"""

EXTRACT_LISTING_DETAIL_PROMPT = """\
You are extracting details from a single Facebook Marketplace listing page.

Extract all available information:
- title: Full listing title
- price: Numeric price as float
- description: Full item description text
- location: Seller location
- seller_name: Seller's display name
- image_urls: List of all image URLs
- posted_at: When posted (e.g., "2 hours ago", "Yesterday")
- external_id: Listing ID from URL

Return a JSON object with these fields (not an array).

Page content:
{page_content}"""
