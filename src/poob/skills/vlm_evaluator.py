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

from poob.skills.models import PriceLookupResult, TriageResult, VisualEnrichment, VLMEvaluation
from poob.utils.content import extract_json, extract_text
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.cache.hash_cache import ResultCache
    from poob.config import AppConfig
    from poob.llm.vlm_cascade import VLMCascade
    from poob.storage.models import Listing

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
{seller_price_section}

## CONDITION SIGNALS (auto-detected from text)
{condition_signals_section}

## ADDITIONAL INTELLIGENCE
{additional_intelligence_section}

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
   **VALIDATE COMPARABLES FIRST:**
   - The comparable search query is shown in quotes above. Compare it to the listing title.
   - If the search query names a DIFFERENT product or premium brand that does NOT appear
     in the listing title/description (e.g., comparables for "Herman Miller Aeron" but
     listing just says "Office chair" with no brand), IGNORE the comparables entirely
     and estimate value from your own knowledge instead.
   - If the listing has NO identifiable brand and the comparables are for a branded item,
     assume a generic/budget version and price accordingly.
   - Generic unbranded items on Facebook Marketplace (chairs, dishes, basic furniture,
     baby items) rarely exceed $50-100 in fair market value unless a specific brand
     is clearly visible in the photos or stated in the listing.
   **If the seller states what they originally paid** (e.g., "Paid $265", "Originally $300"),
   treat that as the MAXIMUM fair market value when new. Apply depreciation from there.
   The item cannot be worth MORE than what was paid for it.
   **After validation:**
   - If comparables are valid: anchor your estimate to those, adjust for condition
   - If retail prices provided: apply depreciation for condition and age
   - If neither (or comparables rejected): use your training knowledge of retail prices
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

CRITICAL RULES:
- A low price alone does NOT make something incredible. A $5 stained mattress is NOT \
a deal. A $200 Herman Miller Aeron with minor wear IS a deal.
- Consider DESIRABILITY. Popular brands, in-demand items, and useful goods are deals. \
Random junk at low prices is not.
- **CHECK DESCRIPTION AND PHOTOS FOR MISLEADING SIGNALS.** Set deal_quality to "pass" \
ONLY if you can clearly see evidence that the item is NOT actually for sale at the listed price: \
  - Description explicitly says "trades only", "looking to trade", "pop up event" \
  - Photos show text overlays like "DM for price", "trades welcome", "contact for pricing" \
  - The listing is clearly an advertisement, event, or service — not an item for sale \
  When the description is empty (very common on FB), use the PHOTOS to look for these signals. \
  **Do NOT assume bad intent just because the price is low.** Many sellers genuinely want to \
  get rid of items quickly at rock-bottom prices — that is exactly the kind of deal we're looking for.
- **DOLLAR SAVINGS MATTER MORE THAN PERCENTAGES.** Saving $9 on a $10 item is NOT \
incredible. Saving $200 on a $400 item IS. Calibrate your deal_quality based on how \
much real money the buyer saves, not just the percentage.
- If USER PREFERENCES are provided (in WATCHLIST MATCH section), they are HARD CONSTRAINTS. \
EVERY preference must be satisfied — not just some. Examples: \
"bulk listings, many cards" = this MUST be a bulk/lot listing with many items, NOT a single item. \
"seller unaware" = the seller must appear unaware of the item's true value. \
"extremely cheap" = the asking price must be extremely low for the category. \
"only cups" = ONLY cups — not vases, plates, bowls, or any other item. \
If ANY preference is not met, deal_quality MUST be "pass". No exceptions.
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

    def __init__(
        self,
        vlm_cascade: VLMCascade,
        config: AppConfig,
        *,
        result_cache: ResultCache | None = None,
    ) -> None:
        self._vlm = vlm_cascade
        self._config = config
        self._cache = result_cache

    async def evaluate(
        self,
        listing: Listing,
        enrichment: VisualEnrichment,
        comparable_prices: PriceLookupResult | None,
        triage: TriageResult,
        watchlist_context: dict[str, Any] | None = None,
        seller_stated_price: float | None = None,
        condition_signals: dict[str, list[str]] | None = None,
        additional_intelligence: dict[str, Any] | None = None,
    ) -> VLMEvaluation:
        """Evaluate a single listing through the VLM.

        Args:
            listing: The marketplace listing.
            enrichment: Visual enrichment data (product name, retail prices, OCR).
            comparable_prices: eBay sold price data (if available).
            triage: Triage signals (urgency, scam, misspelling).
            watchlist_context: Optional watchlist match context.
            seller_stated_price: Price seller states they originally paid.

        Returns:
            VLMEvaluation with deal assessment.
        """
        # Fetch images — "max" effort sends ALL images for thorough OCR/analysis
        effort = (watchlist_context or {}).get("effort", "normal")
        max_images_override = len(listing.image_urls) if effort == "max" else None
        image_parts = await self._fetch_images(
            listing.image_urls, max_images=max_images_override
        )

        # Build the text prompt
        prompt_text = self._build_prompt(
            listing, enrichment, comparable_prices, triage, watchlist_context,
            seller_stated_price=seller_stated_price,
            condition_signals=condition_signals,
            additional_intelligence=additional_intelligence,
        )

        # Check VLM cache: keyed by image phash + prompt hash
        image_phash: str | None = None
        prompt_hash: str | None = None
        if self._cache and listing.image_urls:
            from poob.cache.hash_cache import compute_text_hash

            # Use first image URL as cache key component (stable per listing)
            prompt_hash = compute_text_hash(prompt_text)
            image_phash = compute_text_hash(listing.image_urls[0])
            cached = self._cache.get_vlm(image_phash, prompt_hash)
            if cached:
                log.info("vlm_eval.cache_hit", listing=listing.title[:40])
                return self._dict_to_evaluation(cached)

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
            evaluation = self._parse_response(extract_text(response.content))

            # Use voting agreement confidence when available.
            # - Agreement > 0 means providers agreed — this is a strong signal,
            #   more reliable than any single LLM's self-reported confidence.
            # - Agreement == 0.0 means no consensus (providers disagreed).
            #   In that case, keep the winning provider's self-reported confidence
            #   rather than overriding to 0.0, which would wastefully trigger a
            #   second opinion on every non-consensus evaluation.
            # - None means voting was not used (single provider or sequential mode).
            primary_agreement = self._vlm.last_agreement_confidence
            if primary_agreement is not None and primary_agreement > 0:
                evaluation.confidence = primary_agreement

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
                    evaluation = self._reconcile_opinions(
                        evaluation, second, primary_agreement,
                    )

            # Cache the evaluation result
            if self._cache and image_phash and prompt_hash:
                self._cache.set_vlm(
                    image_phash, prompt_hash,
                    self._evaluation_to_dict(evaluation),
                )

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
        seller_stated_price: float | None = None,
        condition_signals: dict[str, list[str]] | None = None,
        additional_intelligence: dict[str, Any] | None = None,
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
                    f"  *** HARD RULE: EVERY word in USER PREFERENCES is a binding constraint. "
                    f"The listing MUST match ALL of them, not just some. "
                    f"If preferences say 'bulk listings' and this is a single item, "
                    f"deal_quality MUST be 'pass'. "
                    f"If preferences say 'seller unaware' and the seller clearly knows "
                    f"the value (priced near market), deal_quality MUST be 'pass'. "
                    f"If preferences say 'extremely cheap' and the price is not extremely "
                    f"cheap for the category, deal_quality MUST be 'pass'. "
                    f"If preferences say 'not metal' and the item IS metal, "
                    f"deal_quality MUST be 'pass'. "
                    f"Evaluate the listing against EACH preference individually. "
                    f"If ANY single preference is not met, deal_quality = 'pass'. "
                    f"Price and deal percentage are IRRELEVANT if preferences are not met. ***"
                )
            watchlist_section = "\n".join(watchlist_parts)
        else:
            watchlist_section = ""

        if listing.price is not None and listing.price > 0:
            price_str = f"${listing.price}"
        elif listing.price is not None and listing.price == 0:
            price_str = "FREE"
        else:
            price_str = "Not listed (check the listing image for the price)"
        posted_at = str(listing.posted_at) if listing.posted_at else "Unknown"

        # Seller-stated original price section
        if seller_stated_price:
            seller_price_section = (
                f"** Seller states original price: ${seller_stated_price:.2f} **\n"
                f"  This is the MAXIMUM the item could be worth when new. "
                f"Apply depreciation from this price."
            )
        else:
            seller_price_section = ""

        # Condition signals section (auto-detected from text, free intelligence)
        cond = condition_signals or {"positive": [], "negative": []}
        cond_parts: list[str] = []
        if cond["positive"]:
            cond_parts.append(f"Positive: {', '.join(cond['positive'])}")
        if cond["negative"]:
            cond_parts.append(f"Negative: {', '.join(cond['negative'])}")
        condition_signals_section = "\n".join(cond_parts) or "No specific signals detected"

        # Additional intelligence section (model numbers, title quality, freshness, multi-item)
        intel = additional_intelligence or {}
        intel_parts: list[str] = []
        if intel.get("model_numbers"):
            intel_parts.append(
                f"Model numbers detected: {', '.join(intel['model_numbers'])}"
            )
        if intel.get("title_quality_score") is not None:
            tq = intel["title_quality_score"]
            label = "clean" if tq >= 0.8 else "moderate" if tq >= 0.5 else "spammy"
            intel_parts.append(f"Title quality: {tq:.2f} ({label})")
        if intel.get("freshness_bonus") is not None:
            fb = intel["freshness_bonus"]
            label = "just posted" if fb >= 0.9 else "fresh" if fb >= 0.5 else "stale"
            intel_parts.append(f"Listing freshness: {fb:.2f} ({label})")
        if intel.get("multi_item"):
            mi = intel["multi_item"]
            intel_parts.append(
                f"Multi-item listing: {mi['quantity']} items, "
                f"${mi['per_unit_price']:.2f}/each"
            )
        if intel.get("web_context"):
            intel_parts.append(f"Web context: {intel['web_context'][:300]}")
        additional_intelligence_section = (
            "\n".join(intel_parts) if intel_parts else "No additional signals"
        )

        return VLM_EVALUATION_TEMPLATE.format(
            comparable_sales_section=comparable_sales_section,
            enrichment_section=enrichment_section,
            title=listing.title,
            price_str=price_str,
            description=(listing.description or "")[:500] or (
                "[No description available — Facebook does not provide descriptions "
                "in search results. Rely on PHOTOS and TITLE for all assessment. "
                "Check photos carefully for text overlays that reveal the seller's "
                "actual intent (trades, make offer, event, etc.)]"
            ),
            location=listing.location,
            condition_text=listing.raw_data.get("condition", "Not specified"),
            seller_name=listing.seller_name,
            posted_at=posted_at,
            seller_price_section=seller_price_section,
            condition_signals_section=condition_signals_section,
            additional_intelligence_section=additional_intelligence_section,
            urgency_signals=", ".join(triage.urgency_signals) or "None",
            misspelling_bonus="Yes" if triage.misspelling_bonus else "No",
            scam_signals=", ".join(triage.scam_signals) or "None",
            watchlist_section=watchlist_section,
        )

    async def _fetch_images(
        self, image_urls: list[str], *, max_images: int | None = None,
    ) -> list[str]:
        """Download and base64-encode listing images.

        Normally fetches the first and last image (hero + detail).
        When ``max_images`` is provided (e.g. for "max" effort watchlist items),
        fetches up to that many images for thorough OCR/analysis.

        Args:
            image_urls: List of image URLs from the listing.
            max_images: Override for how many images to fetch (None = config default).

        Returns:
            List of data URI strings (may be empty if all fetches fail).
        """
        if not image_urls:
            return []

        effective_max = min(
            max_images if max_images is not None else self._config.vlm_max_images,
            len(image_urls),
        )
        # Select images: for <= 2, use first+last; for more, use all up to max
        if effective_max <= 2:
            urls_to_fetch: list[str] = [image_urls[0]]
            if effective_max >= 2 and len(image_urls) > 1:
                urls_to_fetch.append(image_urls[-1])
        else:
            urls_to_fetch = image_urls[:effective_max]

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

        Uses robust extraction that handles thinking model output,
        markdown fences, and multi-block responses.
        """
        data = extract_json(content)
        if data is None:
            log.warning("vlm.json_parse_failed", content_preview=content[:200])
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
            return self._parse_response(extract_text(response.content))
        except Exception as exc:
            log.debug("second_opinion.failed", error=str(exc)[:100])
            return None

    @staticmethod
    def _reconcile_opinions(
        primary: VLMEvaluation,
        second: VLMEvaluation,
        agreement_confidence: float | None = None,
    ) -> VLMEvaluation:
        """Reconcile two VLM opinions into a final evaluation.

        Uses agreement-based confidence from the primary voting phase when
        available — more reliable than self-reported confidence.

        Args:
            primary: Primary VLM evaluation.
            second: Second opinion evaluation.
            agreement_confidence: Voting agreement confidence from primary eval.
        """
        # Use agreement confidence only when providers actually agreed (> 0).
        # 0.0 means no consensus — fall back to the primary's own confidence.
        if agreement_confidence is not None and agreement_confidence > 0:
            base_conf = agreement_confidence
        else:
            base_conf = primary.confidence

        if primary.deal_quality == second.deal_quality:
            primary.confidence = min(base_conf + 0.2, 1.0)
            primary.reasoning += " [Second opinion agrees]"
            return primary

        # Disagreement — take the more conservative assessment
        quality_rank = {"pass": 0, "fair": 1, "good": 2, "great": 3, "incredible": 4}
        p_rank = quality_rank.get(primary.deal_quality, 0)
        s_rank = quality_rank.get(second.deal_quality, 0)

        if s_rank < p_rank:
            second.reasoning += (
                f" [Downgraded from {primary.deal_quality}: "
                f"second opinion disagrees]"
            )
            second.confidence = max(base_conf, second.confidence) * 0.7
            return second

        primary.reasoning += (
            f" [Second opinion rated {second.deal_quality}, "
            f"keeping conservative {primary.deal_quality}]"
        )
        primary.confidence = max(base_conf, second.confidence) * 0.8
        return primary

    @staticmethod
    def _evaluation_to_dict(evaluation: VLMEvaluation) -> dict:
        """Convert a VLMEvaluation to a cacheable dict."""
        return {
            "item_identified": evaluation.item_identified,
            "condition": evaluation.condition,
            "condition_notes": evaluation.condition_notes,
            "depreciation_factor": evaluation.depreciation_factor,
            "estimated_value_low": evaluation.estimated_value_low,
            "estimated_value_mid": evaluation.estimated_value_mid,
            "estimated_value_high": evaluation.estimated_value_high,
            "deal_quality": evaluation.deal_quality,
            "confidence": evaluation.confidence,
            "reasoning": evaluation.reasoning,
            "red_flags": evaluation.red_flags,
        }

    @staticmethod
    def _dict_to_evaluation(data: dict) -> VLMEvaluation:
        """Reconstruct a VLMEvaluation from a cached dict."""
        return VLMEvaluation(
            item_identified=data.get("item_identified", ""),
            condition=data.get("condition", "unknown"),
            condition_notes=data.get("condition_notes", ""),
            depreciation_factor=data.get("depreciation_factor", 0.6),
            estimated_value_low=data.get("estimated_value_low", 0.0),
            estimated_value_mid=data.get("estimated_value_mid", 0.0),
            estimated_value_high=data.get("estimated_value_high", 0.0),
            deal_quality=data.get("deal_quality", "pass"),
            confidence=data.get("confidence", 0.0),
            reasoning=data.get("reasoning", ""),
            red_flags=data.get("red_flags", []),
        )
