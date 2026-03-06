"""Stage 1: Batched text triage for marketplace listings.

Sends batches of 5-8 listings to a cheap/fast cloud LLM to determine which
are worth investigating further.  Filters ~60-70% of listings before expensive
VLM evaluation.

Provider cascade: Cerebras → Groq → Ollama.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage

from agentic_scraper.skills.llm_call import llm_call
from agentic_scraper.skills.models import TriageResult
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from agentic_scraper.storage.models import Listing, WatchItem

log = get_logger("skills.text_triage")

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
- Item matches a user's watchlist interest (check PREFS if specified)
- Misspelled brand names (seller may not know the value)

INVESTIGATE = false when:
- Price is at or above typical market value for the category
- Listing appears to be spam or keyword stuffing
- Description demands off-platform communication (scam signal)
- Item is clearly junk with no resale value (stained mattress, broken IKEA shelving)
- Price is suspiciously low (<20% of obvious value) with no explanation (bait-and-switch)
- Item matches a watchlist keyword BUT contradicts the user's PREFS — this is a \
HARD RULE: if PREFS say "not metal" and the listing title/description contains "metal", \
set investigate=false REGARDLESS of price. Same for any "not X" / "no X" / "avoid X" in PREFS.

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
        price_str = f"${listing.price}" if listing.price else "FREE"
        desc = (listing.description or "")[:300]
        parts.append(
            f"---\n"
            f"Listing {i}:\n"
            f"  Title: {listing.title}\n"
            f"  Price: {price_str}\n"
            f"  Description: {desc}\n"
            f"  Location: {listing.location}\n"
            f"---"
        )
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
            return _parse_triage_response(str(response.content), listings)
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
