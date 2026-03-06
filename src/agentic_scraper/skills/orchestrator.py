"""SmartDealRadar — VLM-driven deal evaluation pipeline.

Stage 1: Text triage (batch filter via cloud LLM)
Stage 2: Visual enrichment (Google Cloud Vision / SerpAPI) + comparable sales
Stage 3: VLM deep evaluation (Gemini/Groq/OpenRouter/Ollama cascade)
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from agentic_scraper.skills.models import TriageResult, VisualEnrichment, VLMEvaluation
from agentic_scraper.storage.models import Deal, DealScore, Listing
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.skills.ebay_lookup import EbayLookupTool
    from agentic_scraper.skills.retail_lookup import RetailLookupTool
    from agentic_scraper.skills.text_triage import TextTriageService
    from agentic_scraper.skills.visual_enrichment import VisualEnrichmentService
    from agentic_scraper.skills.vlm_evaluator import VLMDealEvaluator
    from agentic_scraper.storage.models import WatchItem

log = get_logger("skills.orchestrator")

# Map deal_quality strings from VLM → DealScore enum
_QUALITY_TO_SCORE: dict[str, DealScore] = {
    "pass": DealScore.FAIR,
    "fair": DealScore.FAIR,
    "good": DealScore.GOOD,
    "great": DealScore.GREAT,
    "incredible": DealScore.INCREDIBLE,
}

_SCORE_RANK: dict[DealScore, int] = {
    DealScore.UNKNOWN: 0,
    DealScore.FAIR: 1,
    DealScore.GOOD: 2,
    DealScore.GREAT: 3,
    DealScore.INCREDIBLE: 4,
}


class SmartDealRadar:
    """VLM-driven deal evaluation pipeline.

    Stage 1: Text triage (batch filter)
    Stage 2: Visual enrichment + comparable sales
    Stage 3: VLM deep evaluation

    Args:
        text_triage: Batched text triage service.
        visual_enrichment: Visual enrichment service (Vision API / SerpAPI).
        vlm_evaluator: VLM deal evaluator with provider cascade.
        ebay_lookup: eBay sold price lookup tool.
        retail_lookup: Retail/MSRP price lookup tool.
        min_deal_quality: Minimum deal quality to create Deal objects.
    """

    def __init__(
        self,
        *,
        text_triage: TextTriageService,
        visual_enrichment: VisualEnrichmentService,
        vlm_evaluator: VLMDealEvaluator,
        ebay_lookup: EbayLookupTool | None = None,
        retail_lookup: RetailLookupTool | None = None,
        min_deal_quality: str = "good",
    ) -> None:
        self._text_triage = text_triage
        self._visual_enrichment = visual_enrichment
        self._vlm_evaluator = vlm_evaluator
        self._ebay_lookup = ebay_lookup
        self._retail_lookup = retail_lookup
        self._min_score = _QUALITY_TO_SCORE.get(min_deal_quality, DealScore.GOOD)

    async def evaluate_batch(
        self,
        listings: list[Listing],
        watchlist_items: list[WatchItem] | None = None,
    ) -> list[tuple[Deal | None, VLMEvaluation]]:
        """Evaluate a batch of listings through the full pipeline.

        Args:
            listings: New listings to evaluate.
            watchlist_items: Active watchlist items for triage context.

        Returns:
            List of (Deal or None, VLMEvaluation) tuples.
        """
        if not listings:
            return []

        t0 = time.monotonic()

        # Stage 1: Text triage (batched)
        log.info("Stage 1: Text triage", listings=len(listings))
        triage_results = await self._text_triage.triage_batch(
            listings, watchlist_items
        )

        # Filter to investigative listings
        investigative = [
            (listing, triage)
            for listing, triage in zip(listings, triage_results)
            if triage.investigate
        ]

        if not investigative:
            log.info("Stage 1: All listings filtered out")
            return [
                (None, VLMEvaluation(reasoning="Filtered by triage"))
                for _ in listings
            ]

        log.info(
            "Stage 1 complete",
            investigate=len(investigative),
            filtered=len(listings) - len(investigative),
        )

        # Stage 2: Visual enrichment + comparable sales (concurrent)
        log.info("Stage 2: Visual enrichment", count=len(investigative))
        enriched = await self._enrich_batch(investigative)

        # Stage 3: VLM deep evaluation (semaphore-throttled)
        log.info("Stage 3: VLM evaluation", count=len(enriched))
        results = await self._evaluate_batch(enriched, watchlist_items)

        elapsed = time.monotonic() - t0
        deals = sum(1 for deal, _ in results if deal is not None)
        log.info(
            "Pipeline complete",
            total_listings=len(listings),
            investigated=len(investigative),
            deals_found=deals,
            elapsed=f"{elapsed:.1f}s",
        )

        return results

    async def evaluate(
        self, listing: Listing
    ) -> tuple[Deal | None, VLMEvaluation]:
        """Evaluate a single listing (convenience wrapper).

        Args:
            listing: The marketplace listing to evaluate.

        Returns:
            Tuple of (Deal if the listing is a deal else None, VLMEvaluation).
        """
        results = await self.evaluate_batch([listing])
        return results[0] if results else (None, VLMEvaluation())

    async def _enrich_batch(
        self,
        items: list[tuple[Listing, TriageResult]],
    ) -> list[tuple[Listing, TriageResult, VisualEnrichment, Any]]:
        """Stage 2: Visual enrichment + comparable sales for each listing."""
        sem = asyncio.Semaphore(3)

        async def _enrich_one(
            listing: Listing, triage: TriageResult
        ) -> tuple[Listing, TriageResult, VisualEnrichment, Any]:
            async with sem:
                # Visual enrichment
                enrichment = VisualEnrichment()
                if listing.image_urls:
                    try:
                        enrichment = await self._visual_enrichment.enrich(
                            listing.image_urls[0]
                        )
                    except Exception as exc:
                        log.warning(
                            "Visual enrichment failed",
                            listing_id=listing.id,
                            error=str(exc)[:100],
                        )

                # Comparable sales (only if we got a real product name)
                comparables = None
                if (
                    enrichment.enriched_product_name
                    and self._ebay_lookup is not None
                ):
                    query = enrichment.enriched_product_name
                    if (
                        enrichment.enriched_brand
                        and enrichment.enriched_brand.lower()
                        not in query.lower()
                    ):
                        query = f"{enrichment.enriched_brand} {query}"
                    try:
                        comparables = await self._ebay_lookup.run(query)
                    except Exception as exc:
                        log.warning(
                            "eBay lookup failed",
                            query=query[:50],
                            error=str(exc)[:100],
                        )

                return listing, triage, enrichment, comparables

        results = await asyncio.gather(
            *(_enrich_one(l, t) for l, t in items),
            return_exceptions=True,
        )

        # Filter out exceptions
        valid: list[tuple[Listing, TriageResult, VisualEnrichment, Any]] = []
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                log.warning(
                    "Enrichment failed for listing",
                    listing_id=items[i][0].id,
                    error=str(r)[:100],
                )
                # Still process it with empty enrichment
                listing, triage = items[i]
                valid.append((listing, triage, VisualEnrichment(), None))
            else:
                valid.append(r)

        return valid

    async def _evaluate_batch(
        self,
        enriched_items: list[tuple[Listing, TriageResult, VisualEnrichment, Any]],
        watchlist_items: list[WatchItem] | None,
    ) -> list[tuple[Deal | None, VLMEvaluation]]:
        """Stage 3: VLM evaluation for each enriched listing."""
        sem = asyncio.Semaphore(2)  # Tighter semaphore for VLM calls

        async def _eval_one(
            listing: Listing,
            triage: TriageResult,
            enrichment: VisualEnrichment,
            comparables: Any,
        ) -> tuple[Deal | None, VLMEvaluation]:
            async with sem:
                # Build watchlist context if applicable
                watchlist_context = _build_watchlist_context(
                    listing, watchlist_items
                )

                try:
                    vlm_result = await self._vlm_evaluator.evaluate(
                        listing=listing,
                        enrichment=enrichment,
                        comparable_prices=comparables,
                        triage=triage,
                        watchlist_context=watchlist_context,
                    )
                except Exception as exc:
                    log.warning(
                        "VLM evaluation failed",
                        listing_id=listing.id,
                        error=str(exc)[:100],
                    )
                    return None, VLMEvaluation(
                        reasoning=f"VLM error: {str(exc)[:80]}"
                    )

                # Convert VLM output to Deal object
                deal = self._vlm_to_deal(listing, vlm_result, watchlist_context)
                return deal, vlm_result

        results = await asyncio.gather(
            *(
                _eval_one(l, t, e, c)
                for l, t, e, c in enriched_items
            ),
            return_exceptions=True,
        )

        # Filter out exceptions
        valid: list[tuple[Deal | None, VLMEvaluation]] = []
        for r in results:
            if isinstance(r, BaseException):
                valid.append((None, VLMEvaluation(reasoning=f"Error: {r}")))
            else:
                valid.append(r)

        return valid

    def _vlm_to_deal(
        self,
        listing: Listing,
        vlm: VLMEvaluation,
        watchlist_context: dict[str, Any] | None,
    ) -> Deal | None:
        """Convert a VLM evaluation to a Deal object (or None if below threshold)."""
        # Hard preference filter: reject listings that contradict user notes
        if watchlist_context and watchlist_context.get("notes"):
            contradiction = _listing_contradicts_notes(
                listing, watchlist_context["notes"]
            )
            if contradiction:
                log.info(
                    "Listing rejected by preference filter",
                    title=listing.title[:50],
                    excluded_term=contradiction,
                    notes=watchlist_context["notes"][:80],
                )
                return None

        score = _QUALITY_TO_SCORE.get(vlm.deal_quality, DealScore.FAIR)

        # Check minimum score threshold
        if _SCORE_RANK.get(score, 0) < _SCORE_RANK.get(self._min_score, 0):
            return None

        # Calculate discount percentage from VLM estimates
        listing_price = listing.price or 0
        discount_pct = 0.0
        if vlm.estimated_value_mid > 0 and listing_price > 0:
            discount_pct = (
                (vlm.estimated_value_mid - listing_price) / vlm.estimated_value_mid
            ) * 100
            discount_pct = max(discount_pct, 0.0)
        elif listing_price <= 0 and vlm.estimated_value_mid > 0:
            discount_pct = 100.0  # Free item with identifiable value

        # Build reasoning
        reasoning = vlm.reasoning
        if vlm.red_flags:
            reasoning += f" Flags: {', '.join(vlm.red_flags)}."

        deal = Deal(
            listing_id=listing.id or "",
            score=score,
            estimated_market_price=vlm.estimated_value_mid,
            discount_pct=round(discount_pct, 1),
            llm_reasoning=reasoning,
        )

        # Attach watchlist item ID if this is a watchlist match
        if watchlist_context:
            deal.watch_item_id = watchlist_context.get("watch_item_id")

        return deal


_NEGATION_PATTERN = re.compile(
    r"\b(?:not?|avoid|never|without|don'?t\s+want)\s+(.+?)(?:[,.\n;]|$)",
    re.IGNORECASE,
)


def _extract_excluded_terms(notes: str) -> list[str]:
    """Extract excluded terms from user preference notes.

    Parses patterns like "not metal", "no IKEA", "avoid particle board",
    "without wheels", "don't want plastic".

    Returns:
        List of lowercased excluded terms.
    """
    if not notes:
        return []

    excluded: list[str] = []
    for match in _NEGATION_PATTERN.finditer(notes):
        # Take up to 3 words after the negation keyword
        term = match.group(1).strip().lower()
        words = term.split()[:3]
        excluded.append(" ".join(words))
    return excluded


def _listing_contradicts_notes(
    listing: Listing,
    notes: str,
) -> str | None:
    """Check if a listing contradicts user preference notes.

    Returns the contradicting term if found, or None if no contradiction.
    """
    excluded = _extract_excluded_terms(notes)
    if not excluded:
        return None

    text = f"{listing.title} {listing.description or ''}".lower()

    for term in excluded:
        # Check for the excluded term as whole words in the listing text
        # Use word boundaries to avoid "metal" matching "metallica"
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, text):
            return term

    return None


def _build_watchlist_context(
    listing: Listing,
    watchlist_items: list[WatchItem] | None,
) -> dict[str, Any] | None:
    """Check if a listing matches any watchlist item and build context."""
    if not watchlist_items:
        return None

    title_lower = listing.title.lower()
    desc_lower = (listing.description or "").lower()

    for item in watchlist_items:
        interest_words = item.interest.lower().split()
        # Check if all interest words appear in title or description
        if all(
            word in title_lower or word in desc_lower
            for word in interest_words
        ):
            ctx: dict[str, Any] = {
                "watch_item_id": item.id,
                "interest": item.interest,
                "max_price": item.max_price,
                "threshold": item.notification_threshold,
            }
            if item.notes:
                ctx["notes"] = item.notes
            return ctx

    return None
