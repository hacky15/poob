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
    "condition": "<new|like new|good|fair|parts>",
    "confidence": <0.0-1.0>,
    "needs_visual": false,
    "urgency_signals": ["<list of seller urgency/motivation phrases found>"]
}}

Focus on:
- What specific product/item is shown — be precise (brand, model, size, variant)
- Any visible brand logos, model numbers, or markings
- The ACTUAL condition based on what you see:
  - "new": sealed box, tags still on, never opened
  - "like new": opened but pristine, no visible wear
  - "good": minor wear, fully functional
  - "fair": noticeable wear, scratches, dents but works
  - "parts": broken, cracked screen, missing pieces, doesn't power on, visibly damaged
- Whether it looks like a stock photo (lower confidence) vs real photo
- Look for damage: cracks, stains, missing parts, rust, torn fabric, broken pieces
- If the item appears broken/non-functional, set condition to "parts"
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

CATEGORY_ESTIMATE_PROMPT = """Estimate what this SPECIFIC item would realistically SELL FOR \
on Facebook Marketplace (a local used goods platform, similar to Craigslist).

Item: {item_name}
Brand: {brand}
Model: {model}
Category: {category}
Condition: {condition}
Listing description: {description_hints}

Respond in JSON only, no additional text:
{{
    "low_price": <float - low end of typical FB Marketplace selling price in USD>,
    "high_price": <float - high end of typical FB Marketplace selling price in USD>,
    "typical_price": <float - most common FB Marketplace selling price in USD>,
    "confidence": <0.0-1.0 how confident you are in this range>
}}

CRITICAL RULES:
1. Price the SPECIFIC item above (brand + model), NOT the generic category.
   Example: "DeLonghi EC155" espresso machine = $50-120, NOT "kitchen appliance" = $20-40.
2. These are LOCAL USED marketplace prices, NOT retail/new prices.
3. Apply depreciation from retail:
   - Electronics: 40-60% of retail when used (1-2 years old)
   - Furniture: 20-40% of retail when used
   - Appliances: 25-45% of retail when used
   - Clothing: 10-30% of retail
   - Toys/Games: 30-50% of retail
   - Tools: 30-50% of retail
   - "parts" or "broken" condition: 5-15% of retail
4. Use your knowledge of the item's retail price to anchor the estimate.
   Think: "What does this item retail for?" → apply depreciation.
5. Facebook Marketplace prices are lower than eBay because: no shipping,
   no fees, no buyer protection, sellers clearing space.
6. When in doubt, estimate conservatively but REALISTICALLY for the specific item."""

DEAL_VALIDATION_PROMPT = """You are reviewing a potential deal from Facebook Marketplace before \
we notify the user. Your job is to catch CLEAR ERRORS only — misidentified items, wrong photos, \
or obvious scams. The user WANTS to see steep discounts.

## Listing
- Title: {title}
- Listed price: ${listing_price}
- Description: {description}
- Condition (from ID step): {condition}

## Our Analysis
- We identified this as: {item_name} (brand: {brand}, confidence: {id_confidence})
- Market price estimate: ${market_price} (source: {price_source})
- Discount: {discount_pct}% off market
- Deal score: {deal_score}

{image_note}

Respond in JSON only, no additional text:
{{
    "is_valid": <true if this deal should be shown to the user, false ONLY if clearly wrong>,
    "reasoning": "<1-2 sentence explanation>"
}}

ONLY REJECT (is_valid=false) if:
- The photo clearly shows a DIFFERENT item than the title describes (e.g., title says "PS5" \
but photo shows a phone case)
- The item is clearly broken/junk but listed as working
- The listing is an obvious scam (stock photos with watermarks, too-vague descriptions that \
could be bait-and-switch)

DO NOT REJECT just because:
- The discount is steep — people sell things cheap on Facebook Marketplace all the time \
(moving, divorce, clearing space, impulse pricing)
- The photo quality is poor — most FB Marketplace photos are low quality
- The description is short — many legitimate listings have minimal descriptions
- The price seems "too good" — that's exactly what the user is looking for

When in doubt, APPROVE. Let the user decide. False negatives (missing a real deal) are \
worse than false positives (showing a questionable one)."""
