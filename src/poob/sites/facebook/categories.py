"""Facebook Marketplace category mappings and relevance filtering.

Facebook Marketplace supports category-scoped searches via URL path:
    /marketplace/category/<slug>/?query=<q>&maxPrice=<p>

This module maps application category names to Facebook URL slugs
and provides a relevance filter to reject listings that don't match
the intended category (e.g. "coffee table book" when searching for furniture).
"""

from __future__ import annotations

from poob.utils.logging import get_logger

log = get_logger("sites.facebook.categories")

# Application category name -> Facebook Marketplace URL slug.
# Facebook uses these slugs in the URL path for category-scoped search.
# Example: /marketplace/category/furniture/?query=coffee+table
CATEGORY_SLUG_MAP: dict[str, str] = {
    "furniture": "furniture",
    "electronics": "electronics",
    "appliances": "appliances",
    "clothing": "apparel",
    "apparel": "apparel",
    "entertainment": "entertainment",
    "home goods": "homegoods",
    "home-goods": "homegoods",
    "homegoods": "homegoods",
    "garden": "garden",
    "outdoor": "garden",
    "sporting goods": "sporting-goods",
    "sporting-goods": "sporting-goods",
    "sports": "sporting-goods",
    "toys": "toys-games",
    "toys & games": "toys-games",
    "toys-games": "toys-games",
    "musical instruments": "musical-instruments",
    "musical-instruments": "musical-instruments",
    "music": "musical-instruments",
    "office": "office-supplies",
    "office supplies": "office-supplies",
    "pets": "pets",
    "pet supplies": "pets",
    "free": "free-stuff",
    "free stuff": "free-stuff",
    "video games": "video-gaming",
    "gaming": "video-gaming",
    "video-gaming": "video-gaming",
    "baby": "family",
    "kids": "family",
    "family": "family",
}

# Category-specific exclusion keywords.
# If a listing title contains any of these tokens AND matches the search
# keywords, it's almost certainly a false positive for that category.
# Format: {category: [(exclude_token, [exceptions])]}
# - exclude_token: word/phrase to reject
# - exceptions: words that override the exclusion (e.g. "bookshelf" overrides "book")
_CATEGORY_EXCLUSIONS: dict[str, list[tuple[str, list[str]]]] = {
    "furniture": [
        ("book", ["bookshelf", "bookcase", "book shelf", "book case"]),
        ("coffee mug", []),
        ("game board", []),
        ("poster", []),
        ("sticker", []),
        ("replacement part", []),
        ("barbie", []),
        ("dollhouse", ["doll house"]),
    ],
    "electronics": [
        ("case only", []),
        ("skin ", []),
        ("sticker", []),
        ("poster", []),
    ],
    "appliances": [
        ("replacement part", []),
        ("manual only", []),
    ],
}


# Keyword patterns → inferred category.
# Used when no explicit category is set on the watchlist item.
# Each entry: (keyword_phrase, category) — matched case-insensitively against query keywords.
# More specific patterns should come before broader ones.
_KEYWORD_CATEGORY_MAP: list[tuple[str, str]] = [
    # Furniture
    ("coffee table", "furniture"),
    ("dining table", "furniture"),
    ("end table", "furniture"),
    ("side table", "furniture"),
    ("nightstand", "furniture"),
    ("night stand", "furniture"),
    ("bookshelf", "furniture"),
    ("bookcase", "furniture"),
    ("dresser", "furniture"),
    ("desk", "furniture"),
    ("couch", "furniture"),
    ("sofa", "furniture"),
    ("loveseat", "furniture"),
    ("recliner", "furniture"),
    ("futon", "furniture"),
    ("bed frame", "furniture"),
    ("headboard", "furniture"),
    ("ottoman", "furniture"),
    ("tv stand", "furniture"),
    ("entertainment center", "furniture"),
    ("shelf", "furniture"),
    ("cabinet", "furniture"),
    ("wardrobe", "furniture"),
    ("armoire", "furniture"),
    ("bench", "furniture"),
    ("chair", "furniture"),
    # Appliances
    ("espresso machine", "appliances"),
    ("coffee maker", "appliances"),
    ("coffee machine", "appliances"),
    ("dishwasher", "appliances"),
    ("refrigerator", "appliances"),
    ("fridge", "appliances"),
    ("microwave", "appliances"),
    ("washing machine", "appliances"),
    ("dryer", "appliances"),
    ("air conditioner", "appliances"),
    ("vacuum", "appliances"),
    ("blender", "appliances"),
    ("toaster", "appliances"),
    ("oven", "appliances"),
    ("stove", "appliances"),
    ("air fryer", "appliances"),
    ("instant pot", "appliances"),
    # Electronics
    ("ps5", "electronics"),
    ("playstation", "electronics"),
    ("xbox", "electronics"),
    ("nintendo switch", "electronics"),
    ("laptop", "electronics"),
    ("macbook", "electronics"),
    ("ipad", "electronics"),
    ("iphone", "electronics"),
    ("samsung galaxy", "electronics"),
    ("monitor", "electronics"),
    ("gpu", "electronics"),
    ("graphics card", "electronics"),
    ("headphones", "electronics"),
    ("speaker", "electronics"),
    ("camera", "electronics"),
    ("drone", "electronics"),
    ("tv", "electronics"),
    ("television", "electronics"),
]


def infer_category(keywords: str) -> str | None:
    """Infer a category from search keywords.

    Uses keyword matching to guess what category the user is searching for.
    This enables the relevance filter even when no explicit category is set
    on the watchlist item.

    Args:
        keywords: Search keywords (e.g. "coffee table", "PS5").

    Returns:
        Inferred category string, or None if no match.
    """
    keywords_lower = keywords.lower().strip()
    for pattern, category in _KEYWORD_CATEGORY_MAP:
        if pattern in keywords_lower:
            return category
    return None


def get_category_slug(category: str | None) -> str | None:
    """Map an application category name to a Facebook Marketplace URL slug.

    Args:
        category: Application category (e.g. "furniture", "electronics").

    Returns:
        Facebook URL slug, or None if no mapping exists.
    """
    if not category:
        return None
    return CATEGORY_SLUG_MAP.get(category.lower().strip())


def is_relevant_to_category(title: str, category: str | None) -> bool:
    """Check if a listing title is relevant to the intended search category.

    Uses keyword exclusion rules to reject obviously irrelevant listings.
    For example, when searching for furniture, "coffee table book" is rejected
    because "book" matches the exclusion pattern and "bookshelf" doesn't apply.

    Args:
        title: Listing title to check.
        category: Intended category (e.g. "furniture"). None means no filtering.

    Returns:
        True if the listing appears relevant, False if obviously irrelevant.
    """
    if not category:
        return True

    exclusions = _CATEGORY_EXCLUSIONS.get(category.lower().strip(), [])
    if not exclusions:
        return True

    title_lower = title.lower()

    for exclude_token, exceptions in exclusions:
        if exclude_token in title_lower:
            # Check if any exception overrides the exclusion
            if any(exc in title_lower for exc in exceptions):
                continue
            log.debug(
                "Listing excluded by category filter",
                title=title,
                category=category,
                matched_token=exclude_token,
            )
            return False

    return True
