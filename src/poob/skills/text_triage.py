"""Stage 1: Batched text triage for marketplace listings.

Sends batches of 5-8 listings to a cheap/fast cloud LLM to determine which
are worth investigating further.  Filters ~60-70% of listings before expensive
VLM evaluation.

Provider cascade: Cerebras → Groq → Ollama.

Includes a minimum investigate floor: if the LLM is too aggressive (< 15%
pass rate), branded/promising rejects are rescued programmatically. This
prevents complete whiffs when models differ in conservatism.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage

from poob.skills.llm_call import llm_call
from poob.skills.models import TriageResult
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from poob.storage.models import Listing, WatchItem

log = get_logger("skills.text_triage")

# Minimum percentage of listings that should survive triage.
# If the LLM filters more aggressively than this, the most promising
# rejects are rescued programmatically.
_MIN_INVESTIGATE_PCT = 0.15  # 15%
_MIN_INVESTIGATE_FLOOR = 3  # Always let at least 3 through

# Brands that signal resale value — used by the rescue heuristic
# to prioritize branded rejects over generic ones.
_TRIAGE_RESCUE_BRANDS = frozenset({
    "apple", "samsung", "sony", "lg", "dell", "hp", "lenovo", "bose", "jbl",
    "kitchenaid", "kitchen aid", "cuisinart", "ninja", "dyson", "breville",
    "vitamix", "keurig", "nespresso", "le creuset", "lodge", "all-clad",
    "herman miller", "steelcase", "ikea", "pottery barn",
    "dewalt", "milwaukee", "makita", "bosch", "ryobi",
    "nike", "adidas", "lululemon", "patagonia", "north face",
    "canon", "nikon", "gopro", "dji", "nintendo", "playstation", "xbox",
    "whirlpool", "ge", "kenmore", "nest", "ring", "peloton",
    "lego", "trek", "fender", "gibson", "yamaha",
    "rae dunn", "pyrex", "corningware", "fiesta", "scentsy",
    "rca", "jensen", "ihome", "oster", "hamilton beach", "chefman",
    "pampered chef", "calphalon", "instant pot", "weber", "traeger",
    "yeti", "hydro flask", "stanley", "cricut", "roomba",
})

TRIAGE_SYSTEM_PROMPT = """\
You are a marketplace listing pre-screener. For each listing below, determine \
if it's worth investigating further as a potential deal. You are the first filter \
— be efficient but catch genuinely good opportunities.

Respond ONLY with a valid JSON array (one object per listing, in order). \
No markdown fences, no explanation outside the JSON."""

TRIAGE_USER_TEMPLATE = """\
{watchlist_section}

LISTINGS TO EVALUATE:
{listings_block}

For each listing, respond with a JSON array (one object per listing, in order):
[
  {{
    "listing_index": 1,
    "investigate": true,
    "reasoning": "brief explanation",
    "scam_signals": [],
    "urgency_signals": [],
    "misspelling_bonus": false
  }}
]

INVESTIGATE = true when:
- Price seems notably below what the item category typically sells for
- Seller shows urgency/motivation (moving, must sell, OBO, need gone)
- Item is free ($0) and appears to have real value
- Item matches a user's watchlist interest AND satisfies ALL PREFS constraints. \
PREFS are ALL hard requirements. "bulk listings, many cards" means only investigate lots/bulk, \
NOT single items. "seller unaware" means the seller must appear unaware of the value. \
"extremely cheap" means the price must be very low. If ANY PREF is unmet, investigate=false.
- Misspelled brand names (seller may not know the value)

INVESTIGATE = false when:
- Price is at or above typical market value for the category
- Listing appears to be spam or keyword stuffing
- Description demands off-platform communication (scam signal)
- Item is clearly junk with no resale value: stained mattresses, \
broken IKEA shelving, stuffed toys, artificial flowers, literal food/candy, \
dollar-store goods, loose hardware, single-use consumables.
- IMPORTANT: Branded items (KitchenAid, Le Creuset, Cuisinart, RCA, HP, etc.) \
should almost always be investigate=true even at modest prices — they have resale \
value that generic items don't. When in doubt about a branded item, investigate=true.
- Price is suspiciously low (<20% of obvious value) with no explanation (bait-and-switch)
- **MISLEADING LISTINGS**: Description says "trades only", "looking to trade", "pop up", \
"vendor event", or otherwise indicates the item is NOT actually for sale at the listed price. \
Sellers who list at $0 but describe trades, auctions, or "make an offer" are bait pricing. \
Set investigate=false.
- Item matches a watchlist keyword BUT contradicts the user's PREFS — this is a \
HARD RULE: if PREFS say "not metal" and the listing title/description contains "metal", \
set investigate=false REGARDLESS of price. Same for any "not X" / "no X" / "avoid X" in PREFS.
- PREFS say "only X" (e.g. "only cups") and the listing is clearly NOT that item type \
(e.g. it's a vase, plate, or bowl). This is also a HARD RULE.

SCAM SIGNALS to flag:
- "Text me at [number]" or "Email me at" (off-platform)
- Demands for Zelle/Venmo/CashApp payment upfront
- Stock photos or watermarked images mentioned in description
- "Shipping only" for items normally sold locally
- Brand new high-value items at extreme discounts with vague descriptions

MISSPELLING BONUS: Set true if the listing title contains misspelled brand names \
or item names that suggest the seller doesn't know what they're selling. \
Examples: "bycicle" (bicycle), "chan saw" (chainsaw), "Cannondal" (Cannondale)."""


def _build_watchlist_section(watchlist_items: list[WatchItem] | None) -> str:
    """Build the watchlist context section for the triage prompt."""
    if not watchlist_items:
        return ""
    lines = [
        "ACTIVE WATCHLIST (investigate if a listing matches, "
        "but REJECT if it contradicts PREFS — e.g. 'not metal' means NO metal items):"
    ]
    for item in watchlist_items:
        price_str = f" (max ${item.max_price})" if item.max_price else ""
        notes_str = f" — PREFS: {item.notes}" if item.notes else ""
        lines.append(f'  - "{item.interest}"{price_str}{notes_str}')
    return "\n".join(lines) + "\n"


def _build_listings_block(listings: list[Listing]) -> str:
    """Format listings into a numbered text block for the triage prompt."""
    parts: list[str] = []
    for i, listing in enumerate(listings, 1):
        if listing.price is not None and listing.price > 0:
            price_str = f"${listing.price}"
        elif listing.price is not None and listing.price == 0:
            price_str = "FREE"
        else:
            price_str = "Price not listed"
        desc = (listing.description or "")[:300]
        condition = listing.raw_data.get("condition", "")
        seller = listing.seller_name or ""
        lines = [
            f"---",
            f"Listing {i}:",
            f"  Title: {listing.title}",
            f"  Price: {price_str}",
        ]
        if desc:
            lines.append(f"  Description: {desc}")
        if condition:
            lines.append(f"  Condition: {condition}")
        # Freshness context helps triage weight urgency by recency
        if listing.posted_at:
            from datetime import datetime, timezone

            age = datetime.now(timezone.utc) - listing.posted_at
            hours = age.total_seconds() / 3600
            if hours < 1:
                posted_str = f"{int(hours * 60)} minutes ago"
            elif hours < 24:
                posted_str = f"{hours:.1f} hours ago"
            else:
                posted_str = f"{hours / 24:.1f} days ago"
            lines.append(f"  Posted: {posted_str}")
        else:
            lines.append(f"  Posted: Unknown")
        lines.append(f"  Location: {listing.location}")
        if seller:
            lines.append(f"  Seller: {seller}")
        lines.append(f"---")
        parts.append("\n".join(lines))
    return "\n".join(parts)


def _parse_triage_response(
    content: str, listings: list[Listing]
) -> list[TriageResult]:
    """Parse the LLM's JSON array response into TriageResult objects.

    Handles common LLM output quirks: markdown fences, trailing commas,
    partial responses.

    Args:
        content: Raw LLM response text.
        listings: The listings that were triaged (for ID mapping).

    Returns:
        List of TriageResult, one per listing.  Missing entries default to
        investigate=True (fail open — never silently drop a listing).
    """
    text = content.strip()

    # Strip <think>...</think> tags (qwen3, deepseek, and similar models wrap responses)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip <reasoning>...</reasoning> tags (some models use this)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL).strip()

    if not text or text.lower() in ("none", "null", "n/a"):
        log.warning("Triage returned empty response, marking all as investigate=True")
        return [
            TriageResult(listing_id=l.id or "", investigate=True, reasoning="empty_response")
            for l in listings
        ]

    # Try multiple JSON extraction strategies
    items = None

    # Strategy 1: Extract from markdown fence (```json ... ```)
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        try:
            items = json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Strategy 2: Find JSON array directly in text
    if items is None:
        arr_match = re.search(r"\[.*\]", text, re.DOTALL)
        if arr_match:
            try:
                items = json.loads(arr_match.group(0))
            except json.JSONDecodeError:
                pass

    # Strategy 3: Try the whole text as JSON
    if items is None:
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            log.warning(
                "Triage JSON parse failed, marking all as investigate=True",
                response_preview=text[:200],
            )
            return [
                TriageResult(
                    listing_id=l.id or "", investigate=True, reasoning="parse_error"
                )
                for l in listings
            ]

    if not isinstance(items, list):
        items = [items]

    # Build lookup by listing_index
    by_index: dict[int, dict] = {}
    for item in items:
        idx = item.get("listing_index", 0)
        by_index[idx] = item

    results: list[TriageResult] = []
    for i, listing in enumerate(listings, 1):
        entry = by_index.get(i, {})
        results.append(
            TriageResult(
                listing_id=listing.id or "",
                investigate=entry.get("investigate", True),
                reasoning=entry.get("reasoning", ""),
                scam_signals=entry.get("scam_signals", []),
                urgency_signals=entry.get("urgency_signals", []),
                misspelling_bonus=entry.get("misspelling_bonus", False),
            )
        )
    return results


def _rescue_promising_rejects(
    listings: list[Listing],
    results: list[TriageResult],
    target: int,
) -> int:
    """Rescue the most promising rejected listings when the LLM is too aggressive.

    Scores each rejected listing by heuristic signals (brand recognition, price,
    seller urgency) and flips the top ``target`` rejects to investigate=True.

    Args:
        listings: Original listings (same order as results).
        results: Triage results to mutate in-place.
        target: Number of rejects to rescue.

    Returns:
        Number of listings actually rescued.
    """
    # Build (index, score) for each rejected listing
    reject_scores: list[tuple[int, float]] = []
    for i, (listing, result) in enumerate(zip(listings, results)):
        if result.investigate:
            continue

        score = 0.0
        title_lower = (listing.title or "").lower()
        desc_lower = (listing.description or "").lower()
        text = f"{title_lower} {desc_lower}"

        # Brand recognition is the strongest signal
        for brand in _TRIAGE_RESCUE_BRANDS:
            if brand in text:
                score += 50.0
                break

        # Higher price = more deal potential (log scale to avoid bias)
        price = listing.price or 0.0
        if price >= 20:
            score += min(price / 5, 30.0)
        elif price > 0:
            score += price / 2

        # Urgency signals suggest motivated seller
        urgency_words = ("must sell", "moving", "obo", "need gone", "make offer", "asap")
        if any(w in text for w in urgency_words):
            score += 15.0

        # Model/part numbers suggest specific, identifiable items
        if re.search(r"[A-Z]{2,}\d{2,}", listing.title or ""):
            score += 10.0

        # Penalize obvious garbage
        garbage_signals = ("free", "scammer", "snow removal", "trades only")
        if any(g in title_lower for g in garbage_signals):
            score -= 100.0

        if score > 0:
            reject_scores.append((i, score))

    # Sort by score descending, rescue the top N
    reject_scores.sort(key=lambda x: x[1], reverse=True)
    rescued = 0
    for idx, score in reject_scores[:target]:
        results[idx].investigate = True
        results[idx].reasoning += f" [rescued: score={score:.0f}]"
        rescued += 1
        log.debug(
            "triage.rescued",
            title=listings[idx].title[:50],
            price=listings[idx].price,
            score=round(score, 1),
        )

    return rescued


class TextTriageService:
    """Batched text triage — first stage of the deal evaluation pipeline.

    Sends batches of listings to a cheap/fast LLM to filter out obvious
    non-deals before expensive VLM evaluation.

    Args:
        llm: LangChain chat model (preferably Cerebras → Groq → Ollama chain).
        batch_size: Max listings per LLM call (default 5).
    """

    def __init__(self, llm: BaseChatModel, batch_size: int = 5) -> None:
        self._llm = llm
        self._batch_size = batch_size

    async def triage_batch(
        self,
        listings: list[Listing],
        watchlist_items: list[WatchItem] | None = None,
    ) -> list[TriageResult]:
        """Triage a batch of listings, returning one TriageResult per listing.

        Splits into sub-batches of ``batch_size`` and calls the LLM for each.
        Listings with scam signals set to auto-discard.

        Args:
            listings: Listings to triage.
            watchlist_items: Active watchlist items for context.

        Returns:
            List of TriageResult, same length and order as ``listings``.
        """
        if not listings:
            return []

        all_results: list[TriageResult] = []
        watchlist_section = _build_watchlist_section(watchlist_items)

        for start in range(0, len(listings), self._batch_size):
            batch = listings[start : start + self._batch_size]
            results = await self._triage_one_batch(batch, watchlist_section)
            all_results.extend(results)

        # Post-processing: auto-reject listings with critical scam signals
        for result in all_results:
            if result.scam_signals and not result.investigate:
                continue
            # If scam signals are severe, override investigate to False
            severe_scam = any(
                kw in sig.lower()
                for sig in result.scam_signals
                for kw in ("off-platform", "zelle upfront", "gift card", "wire transfer")
            )
            if severe_scam:
                result.investigate = False
                result.reasoning = f"Auto-rejected: {', '.join(result.scam_signals)}"

        investigated = sum(1 for r in all_results if r.investigate)

        # Minimum investigate floor: rescue promising rejects if the LLM
        # was too aggressive.  Different models vary wildly (Cerebras filters
        # 97%, Groq 33%) — this ensures we always evaluate enough listings.
        min_target = max(_MIN_INVESTIGATE_FLOOR, int(len(listings) * _MIN_INVESTIGATE_PCT))
        if investigated < min_target:
            rescued = _rescue_promising_rejects(
                listings, all_results, target=min_target - investigated,
            )
            if rescued:
                investigated += rescued
                log.info(
                    "triage.rescue_applied",
                    rescued=rescued,
                    new_investigate=investigated,
                    reason="LLM filter rate exceeded safety threshold",
                )

        log.info(
            "triage.complete",
            total=len(listings),
            investigate=investigated,
            filtered=len(listings) - investigated,
        )
        return all_results

    async def _triage_one_batch(
        self,
        listings: list[Listing],
        watchlist_section: str,
    ) -> list[TriageResult]:
        """Send a single batch to the LLM and parse the response."""
        listings_block = _build_listings_block(listings)
        user_msg = TRIAGE_USER_TEMPLATE.format(
            watchlist_section=watchlist_section,
            listings_block=listings_block,
        )

        messages = [
            SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ]

        try:
            response = await llm_call(self._llm, messages, skill="text_triage")
            results = _parse_triage_response(str(response.content), listings)
            # Log per-listing decisions for debugging triage aggressiveness
            for listing, result in zip(listings, results):
                log.debug(
                    "triage.decision",
                    title=listing.title[:50],
                    price=listing.price,
                    investigate=result.investigate,
                    reasoning=result.reasoning[:80],
                )
            return results
        except Exception as exc:
            log.warning(
                "triage batch failed, marking all as investigate=True",
                error=str(exc)[:200],
            )
            return [
                TriageResult(
                    listing_id=l.id or "",
                    investigate=True,
                    reasoning="triage_error",
                )
                for l in listings
            ]
