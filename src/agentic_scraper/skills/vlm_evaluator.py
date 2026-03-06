"""Stage 3: VLM-driven deal evaluation.

The VLM is the primary deal evaluator. It receives:
- 2 listing images (hero + detail)
- Full listing metadata
- Visual enrichment context (product name, retail prices, OCR)
- Comparable sold prices from eBay
- Triage signals (urgency, misspellings, scam flags)
- Watchlist context (if applicable)

And outputs a structured deal assessment with condition, value estimate,
deal quality classification, and red flags.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from agentic_scraper.skills.models import PriceLookupResult, TriageResult, VisualEnrichment, VLMEvaluation
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig
    from agentic_scraper.llm.vlm_cascade import VLMCascade
    from agentic_scraper.storage.models import Listing

log = get_logger("skills.vlm_evaluator")

VLM_SYSTEM_PROMPT = """\
You are an expert marketplace deal evaluator and consumer goods appraiser. Your job is \
to analyze Facebook Marketplace listing photos and metadata, assess the item's true \
condition and value, and determine if it represents a genuine deal worth notifying a user \
about.

You must output ONLY valid JSON matching the schema below. No markdown, no conversation."""

VLM_EVALUATION_TEMPLATE = """\
## COMPARABLE RECENT SALES
{comparable_sales_section}

## VISUAL ENRICHMENT
{enrichment_section}

## LISTING DETAILS
Title: {title}
Listed Price: {price_str}
Description: {description}
Location: {location}
Condition (seller-stated): {condition_text}
Seller: {seller_name}
Posted: {posted_at}

## TRIAGE SIGNALS
Urgency: {urgency_signals}
Misspelling bonus: {misspelling_bonus}
Scam signals: {scam_signals}

{watchlist_section}

## YOUR TASK
Think step by step:
1. IDENTIFY: What exact item is shown? (brand, model, specs, size, variant)
   Use the images as primary evidence. Cross-reference with listing text and enrichment.
2. VERIFY: Does the photo match what the title/description claims?
   If mismatched, flag immediately.
3. CONDITION: Assess the ACTUAL condition from what you see in the photos.
   - mint: sealed, tags on, never opened
   - excellent: opened but pristine, no visible wear
   - good: minor wear, fully functional, light scratches
   - fair: noticeable wear, dents, scratches, but works
   - poor: heavy wear, significant damage, missing parts
   - parts: broken, non-functional, for parts only
   Note specific observations (cracks, stains, rust, missing components).
4. VALUE: Estimate fair market value on Facebook Marketplace (local used market).
   - If comparables provided: anchor your estimate to those, adjust for condition
   - If retail prices provided: apply depreciation for condition and age
   - If neither: use your training knowledge of retail prices, apply depreciation
   - Facebook Marketplace sells 15-25% below eBay (no shipping, cash, smaller buyer pool)
   Provide a range: low (quick sale), mid (fair price), high (patient seller)
5. DEAL ASSESSMENT: Compare listing price to your mid estimate.
   Both the PERCENTAGE discount AND the DOLLAR savings matter.
   A $25 TV stand at 50% off is a decent deal, NOT incredible — you're only saving $25.
   A $200 Herman Miller chair at 50% off IS incredible — you're saving $200.

   Thresholds (BOTH percentage AND minimum dollar savings must be met):
   - pass: listing price >= mid value (not a deal)
   - fair: 5-15% below mid
   - good: 15-30% below mid AND at least $15 saved
   - great: 30-50% below mid AND at least $40 saved
   - incredible: 50%+ below mid AND at least $75 saved, OR free with identifiable value >$50
6. RED FLAGS: Check for scam indicators.
   - Photo shows different item than described
   - Stock photos or watermarked images
   - Price suspiciously low with no explanation
   - Item appears broken but listed as working
   - Too-good-to-be-true brand new luxury items

IMPORTANT NUANCE:
- A low price alone does NOT make something incredible. A $5 stained mattress is NOT \
a deal. A $200 Herman Miller Aeron with minor wear IS a deal.
- Consider DESIRABILITY. Popular brands, in-demand items, and useful goods are deals. \
Random junk at low prices is not.
- Poor photography is NOT a red flag — most FB Marketplace photos are low quality. \
Poor photos often indicate amateur sellers who price things low.
- Short descriptions are NOT a red flag — many legitimate listings have minimal text.
- Steep discounts are NOT inherently suspicious — people sell cheap when moving, \
divorcing, downsizing, or just clearing space. That's what the user is looking for.

Respond with ONLY this JSON (no markdown fences, no text before or after):
{{
  "item_identified": "<specific item: brand, model, key specs>",
  "condition": "<mint|excellent|good|fair|poor|parts>",
  "condition_notes": "<specific observations from the photos>",
  "depreciation_factor": <float 0.1-1.0>,
  "estimated_value_low": <float>,
  "estimated_value_mid": <float>,
  "estimated_value_high": <float>,
  "deal_quality": "<pass|fair|good|great|incredible>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<1-3 sentence explanation of the deal assessment>",
  "red_flags": ["<list of concerns, empty array if none>"]
}}"""


class VLMDealEvaluator:
    """VLM-driven deal evaluator.

    Sends listing images + full context to a VLM for chain-of-thought
    deal assessment.  Supports confidence-based second opinions.

    Args:
        vlm_cascade: Rate-limit-aware VLM provider cascade.
        config: Application configuration.
    """

    def __init__(self, vlm_cascade: VLMCascade, config: AppConfig) -> None:
        self._vlm = vlm_cascade
        self._config = config

    async def evaluate(
        self,
        listing: Listing,
        enrichment: VisualEnrichment,
        comparable_prices: PriceLookupResult | None,
        triage: TriageResult,
        watchlist_context: dict[str, Any] | None = None,
    ) -> VLMEvaluation:
        """Evaluate a single listing through the VLM.

        Args:
            listing: The marketplace listing.
            enrichment: Visual enrichment data (product name, retail prices, OCR).
            comparable_prices: eBay sold price data (if available).
            triage: Triage signals (urgency, scam, misspelling).
            watchlist_context: Optional watchlist match context.

        Returns:
            VLMEvaluation with deal assessment.
        """
        # Fetch images (first + last, max 2)
        image_parts = await self._fetch_images(listing.image_urls)

        # Build the text prompt
        prompt_text = self._build_prompt(
            listing, enrichment, comparable_prices, triage, watchlist_context
        )

        # Construct multimodal message
        content_parts: list[dict[str, Any]] = []

        # Add images first (VLMs perform better with images before text)
        for img_data in image_parts:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": img_data},
            })

        content_parts.append({"type": "text", "text": prompt_text})

        messages = [
            SystemMessage(content=VLM_SYSTEM_PROMPT),
            HumanMessage(content=content_parts),
        ]

        try:
            response = await self._vlm.invoke(messages, skill="vlm_eval")
            evaluation = self._parse_response(str(response.content))

            # Confidence-based second opinion
            if (
                self._config.vlm_second_opinion_enabled
                and evaluation.confidence < self._config.vlm_confidence_threshold
                and evaluation.deal_quality in ("great", "incredible")
            ):
                log.info(
                    "vlm.second_opinion_requested",
                    confidence=evaluation.confidence,
                    deal_quality=evaluation.deal_quality,
                )
                second = await self._get_second_opinion(messages)
                if second:
                    evaluation = self._reconcile_opinions(evaluation, second)

            return evaluation

        except Exception as exc:
            log.warning("vlm_eval.failed", error=str(exc)[:200])
            return VLMEvaluation(
                item_identified=listing.title,
                deal_quality="pass",
                confidence=0.0,
                reasoning=f"VLM evaluation failed: {str(exc)[:100]}",
            )

    def _build_prompt(
        self,
        listing: Listing,
        enrichment: VisualEnrichment,
        comparable_prices: PriceLookupResult | None,
        triage: TriageResult,
        watchlist_context: dict[str, Any] | None,
    ) -> str:
        """Build the full VLM evaluation prompt with all context."""
        # Comparable sales section
        if comparable_prices and comparable_prices.sample_count >= 1:
            comparable_sales_section = (
                f'Comparable eBay sold prices for "{comparable_prices.search_query}":\n'
                f"  Median: ${comparable_prices.median_price:.2f}\n"
                f"  Range: ${comparable_prices.min_price:.2f} - ${comparable_prices.max_price:.2f}\n"
                f"  Sample count: {comparable_prices.sample_count}\n"
                f"  Note: Facebook Marketplace sells 15-25% below eBay "
                f"(no shipping, cash, smaller pool)."
            )
        else:
            comparable_sales_section = (
                "No comparable sales data available — use your training "
                "knowledge and the images to estimate value."
            )

        # Enrichment section
        enrichment_parts: list[str] = []
        if enrichment.enriched_product_name:
            enrichment_parts.append(
                f"Identified product: {enrichment.enriched_product_name}"
            )
        if enrichment.enriched_brand:
            enrichment_parts.append(f"Brand: {enrichment.enriched_brand}")
        if enrichment.enriched_model:
            enrichment_parts.append(f"Model: {enrichment.enriched_model}")
        if enrichment.retail_prices:
            prices_str = ", ".join(f"${p:.2f}" for p in enrichment.retail_prices)
            enrichment_parts.append(f"Retail prices found: {prices_str}")
        if enrichment.ocr_text:
            enrichment_parts.append(
                f"OCR text from image: {enrichment.ocr_text[:200]}"
            )
        if enrichment.web_entities:
            entities_str = ", ".join(
                e["name"] for e in enrichment.web_entities[:5] if e.get("name")
            )
            if entities_str:
                enrichment_parts.append(f"Web entities: {entities_str}")

        if enrichment_parts:
            enrichment_section = (
                f"Enrichment tier: {enrichment.enrichment_tier} "
                f"(1=Vision API, 2=Google Lens, 3=none)\n"
                + "\n".join(enrichment_parts)
            )
        else:
            enrichment_section = (
                "No visual enrichment available — identify from images "
                "and listing text alone."
            )

        # Watchlist section
        if watchlist_context:
            watchlist_parts = [
                f"WATCHLIST MATCH:\n"
                f"  User is looking for: {watchlist_context.get('interest', '')}\n"
                f"  Max budget: ${watchlist_context.get('max_price', 'no limit')}\n"
                f"  Notification level: {watchlist_context.get('threshold', 'good')}",
            ]
            if watchlist_context.get("notes"):
                watchlist_parts.append(
                    f"  USER PREFERENCES: {watchlist_context['notes']}\n"
                    f"  *** HARD RULE: If this listing contradicts ANY user preference "
                    f"above, you MUST set deal_quality to 'pass'. This is NON-NEGOTIABLE. "
                    f"Example: if preferences say 'not metal' and the item IS metal, "
                    f"deal_quality MUST be 'pass'. No exceptions. Price is irrelevant. ***"
                )
            watchlist_section = "\n".join(watchlist_parts)
        else:
            watchlist_section = ""

        price_str = f"${listing.price}" if listing.price else "FREE"
        posted_at = str(listing.posted_at) if listing.posted_at else "Unknown"

        return VLM_EVALUATION_TEMPLATE.format(
            comparable_sales_section=comparable_sales_section,
            enrichment_section=enrichment_section,
            title=listing.title,
            price_str=price_str,
            description=(listing.description or "")[:500],
            location=listing.location,
            condition_text=listing.raw_data.get("condition", "Not specified"),
            seller_name=listing.seller_name,
            posted_at=posted_at,
            urgency_signals=", ".join(triage.urgency_signals) or "None",
            misspelling_bonus="Yes" if triage.misspelling_bonus else "No",
            scam_signals=", ".join(triage.scam_signals) or "None",
            watchlist_section=watchlist_section,
        )

    async def _fetch_images(
        self, image_urls: list[str]
    ) -> list[str]:
        """Download and base64-encode listing images.

        Fetches the first and last image (hero + detail).  Returns
        data URIs suitable for LangChain multimodal messages.

        Args:
            image_urls: List of image URLs from the listing.

        Returns:
            List of data URI strings (may be empty if all fetches fail).
        """
        if not image_urls:
            return []

        max_images = min(self._config.vlm_max_images, len(image_urls))
        # Select first and last (if different)
        urls_to_fetch: list[str] = [image_urls[0]]
        if max_images >= 2 and len(image_urls) > 1:
            urls_to_fetch.append(image_urls[-1])

        results: list[str] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for url in urls_to_fetch:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()

                    content_type = resp.headers.get("content-type", "image/jpeg")
                    if ";" in content_type:
                        content_type = content_type.split(";")[0].strip()

                    b64 = base64.b64encode(resp.content).decode("ascii")
                    results.append(f"data:{content_type};base64,{b64}")
                except Exception as exc:
                    log.debug(
                        "image_fetch.failed",
                        url=url[:80],
                        error=str(exc)[:100],
                    )

        log.info(
            "images.fetched",
            requested=len(urls_to_fetch),
            succeeded=len(results),
        )
        return results

    def _parse_response(self, content: str) -> VLMEvaluation:
        """Parse VLM JSON response into a VLMEvaluation.

        Handles markdown fences and malformed responses gracefully.
        """
        text = content.strip()

        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Try to find JSON object in the response
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("vlm.json_parse_failed", content_preview=text[:200])
            return VLMEvaluation(
                reasoning="Failed to parse VLM response",
                confidence=0.0,
            )

        valid_conditions = {"mint", "excellent", "good", "fair", "poor", "parts", "unknown"}
        valid_qualities = {"pass", "fair", "good", "great", "incredible"}

        condition = str(data.get("condition", "unknown")).lower()
        if condition not in valid_conditions:
            condition = "unknown"

        deal_quality = str(data.get("deal_quality", "pass")).lower()
        if deal_quality not in valid_qualities:
            deal_quality = "pass"

        return VLMEvaluation(
            item_identified=str(data.get("item_identified", "")),
            condition=condition,
            condition_notes=str(data.get("condition_notes", "")),
            depreciation_factor=float(data.get("depreciation_factor", 0.6)),
            estimated_value_low=float(data.get("estimated_value_low", 0.0)),
            estimated_value_mid=float(data.get("estimated_value_mid", 0.0)),
            estimated_value_high=float(data.get("estimated_value_high", 0.0)),
            deal_quality=deal_quality,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            red_flags=list(data.get("red_flags", [])),
        )

    async def _get_second_opinion(
        self, messages: list[Any]
    ) -> VLMEvaluation | None:
        """Request a second opinion from a different VLM provider.

        Only called when primary confidence is low on a high-value deal.
        """
        try:
            response = await self._vlm.invoke(messages, skill="vlm_second_opinion")
            return self._parse_response(str(response.content))
        except Exception as exc:
            log.debug("second_opinion.failed", error=str(exc)[:100])
            return None

    @staticmethod
    def _reconcile_opinions(
        primary: VLMEvaluation, second: VLMEvaluation
    ) -> VLMEvaluation:
        """Reconcile two VLM opinions into a final evaluation.

        If both agree on deal quality, boost confidence.
        If they disagree, use the more conservative one.
        """
        if primary.deal_quality == second.deal_quality:
            # Agreement — boost confidence
            primary.confidence = min(primary.confidence + 0.2, 1.0)
            primary.reasoning += " [Second opinion agrees]"
            return primary

        # Disagreement — take the more conservative assessment
        quality_rank = {"pass": 0, "fair": 1, "good": 2, "great": 3, "incredible": 4}
        p_rank = quality_rank.get(primary.deal_quality, 0)
        s_rank = quality_rank.get(second.deal_quality, 0)

        if s_rank < p_rank:
            # Second opinion is more conservative — use it
            second.reasoning += (
                f" [Downgraded from {primary.deal_quality}: "
                f"second opinion disagrees]"
            )
            second.confidence = max(primary.confidence, second.confidence) * 0.7
            return second

        # Primary is already more conservative
        primary.reasoning += (
            f" [Second opinion rated {second.deal_quality}, "
            f"keeping conservative {primary.deal_quality}]"
        )
        primary.confidence = max(primary.confidence, second.confidence) * 0.8
        return primary
