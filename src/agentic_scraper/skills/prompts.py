"""Prompts for all deal radar skills."""

from __future__ import annotations

IDENTIFY_ITEM_PROMPT = """Analyze this marketplace listing and identify the item being sold.

Title: {title}
Description: {description}

Respond in JSON only, no additional text:
{{
    "item_name": "<specific product name, as precise as possible>",
    "brand": "<brand name or null if unknown>",
    "model": "<model number/name or null if unknown>",
    "category": "<hierarchical category like electronics/gaming/console or furniture/table>",
    "condition": "<new|like new|good|fair|parts or null if unclear>",
    "confidence": <0.0-1.0 how confident you are in this identification>,
    "needs_visual": <true if the text is too vague to identify the item confidently>,
    "urgency_signals": ["<list of seller urgency/motivation phrases found in the listing>"]
}}

IMPORTANT:
- Be precise: "PS5 controller" is an accessory, not a console.
- "For parts", "as-is", "broken" indicate "parts" condition.
- If the title is very vague (e.g. "Nice table"), set confidence low and needs_visual to true.
- Category should be hierarchical: "electronics/phone", "furniture/table/dining", etc.

URGENCY SIGNALS — look for any of these patterns in the title or description and include \
them in urgency_signals (use the exact short label, not the full text):
- "free" — item is listed for free or $0
- "fcfs" or "first come first serve"
- "must sell" or "must go" or "has to go"
- "need gone" or "needs to go" or "need it gone"
- "moving sale" or "moving out" or "relocating"
- "make offer" or "obo" or "or best offer"
- "price drop" or "reduced" or "lowered price"
- "priced to sell" or "steal" or "below cost"
- "garage sale" or "estate sale" or "yard sale"
- "downsizing" or "decluttering" or "spring cleaning"
- "urgent" or "asap" or "today only" or "this weekend only"
- "husband says" or "wife says" or "spouse says" (forced sale)
- "divorce" or "breakup" (liquidation)
- "no longer need" or "don't use" or "never used" or "still in box"
- "curb alert" or "porch pickup" or "come get it"
Return an empty list [] if no urgency signals are found."""

VISUAL_IDENTIFY_PROMPT = """Look at this image of a marketplace listing item and identify what is being sold.

Listing title: {title}
Listing description: {description}

Respond in JSON only, no additional text:
{{
    "item_name": "<specific product name>",
    "brand": "<brand name or null>",
    "model": "<model or null>",
    "category": "<hierarchical category>",
    "condition": "<new|like new|good|fair|parts or null>",
    "confidence": <0.0-1.0>,
    "needs_visual": false,
    "urgency_signals": ["<list of seller urgency/motivation phrases found>"]
}}

Focus on:
- What specific product/item is shown
- Any visible brand logos or markings
- The apparent condition
- Whether it looks like a stock photo (lower confidence) vs real photo
- Urgency signals in the title/description: "free", "fcfs", "must sell", "need gone", \
"moving sale", "obo", "make offer", "price drop", "garage sale", "curb alert", "asap", etc.
Return urgency_signals as an empty list [] if none found."""

RETAIL_PRICE_EXTRACT_PROMPT = """Extract the retail/MSRP price from this web search result text.

Item: {item_name}
Brand: {brand}
Model: {model}

Search result text:
{search_text}

Respond in JSON only, no additional text:
{{
    "retail_price": <float price in USD, or 0 if not found>,
    "source_description": "<where the price came from>",
    "confidence": <0.0-1.0 how confident this is the correct MSRP>
}}

IMPORTANT:
- Only extract prices that clearly refer to this specific item.
- Prefer current retail/MSRP prices over sale prices.
- If multiple prices found, use the most common/official one.
- Return 0 if no reliable price can be determined."""

CATEGORY_ESTIMATE_PROMPT = """Estimate the typical used market price range for this type of item.

Category: {category}
Condition: {condition}
Additional details: {description_hints}

Respond in JSON only, no additional text:
{{
    "low_price": <float - low end of typical price range in USD>,
    "high_price": <float - high end of typical price range in USD>,
    "typical_price": <float - most common price point in USD>,
    "confidence": <0.0-1.0 how confident you are in this range>
}}

Use these depreciation guidelines:
- Electronics: 15-25% depreciation year 1, then 10-15%/year
- Furniture: 30-50% of retail when used
- Clothing: 20-40% of retail
- Vehicles: standard depreciation curves
- "parts" or "broken" condition: 10-30% of working price

Be conservative. It's better to estimate a wider range than a wrong narrow one."""
