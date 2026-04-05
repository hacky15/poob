"""Detail page extractor for Facebook Marketplace listings.

Navigates to individual listing pages and extracts enriched data using a
three-tier strategy:
  Tier 1: GraphQL data from data-sjs script tags (ScheduledServerJS payloads)
  Tier 2: DOM structural selectors (accessibility-stable elements)
  Tier 3: Page markdown text parsing (freshness badges, seller info)

Facebook embeds listing data in data-sjs script tags in the initial HTML —
NOT as separate XHR GraphQL calls. Tier 1 is the primary extraction method.

IMPORTANT: browser-use's Page.evaluate() returns JSON-stringified strings,
not Python objects. All evaluate results must be parsed with
_parse_evaluate_result() before use. This was the root cause of months of
enrichment=0 failures (see docs/technical_notes.md).

OG meta tags and JSON-LD are NOT used — they return empty for regular
browser user agents. See docs/research/fb-detail-page-extraction.md.
"""

from __future__ import annotations

from dataclasses import replace

from agentic_scraper.browser.page_actions import navigate_and_wait
from agentic_scraper.sites.facebook.detail_graphql_extractor import (
    EXTRACT_DATA_SJS_JS,
    EXTRACT_DOM_DETAIL_JS,
    DetailPageData,
    parse_data_sjs_payloads,
    parse_dom_detail,
)
from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.content import parse_evaluate_result as _parse_evaluate_result
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.facebook.detail_extractor")



async def extract_listing_details(
    page: object,
    listing: Listing,
) -> Listing:
    """Navigate to a listing page and enrich it with data-sjs + DOM data.

    Three-tier extraction:
      1. data-sjs script tags containing GraphQL payloads (primary)
      2. DOM structural selectors (fills gaps from Tier 1)
      3. Page markdown text parsing (freshness badges, seller info)

    Args:
        page: A browser-use Page instance.
        listing: Listing with listing_url set.

    Returns:
        A new Listing with enriched data, or the original if extraction fails.
    """
    if not listing.listing_url:
        return listing

    try:
        import asyncio

        detail = DetailPageData()
        extraction_tier = "none"

        # Navigate and wait for React to hydrate + data-sjs tags to populate.
        await navigate_and_wait(page, listing.listing_url, wait_ms=2000)

        # Tier 1: Search ALL script tags and page source for marketplace data.
        # Facebook embeds listing data in data-sjs tags OR in the initial HTML
        # as ScheduledServerJS payloads. The keywords may appear in various formats.
        if not detail.title and not detail.description:
            for _attempt in range(3):
                try:
                    # First try the targeted data-sjs extraction
                    sjs_payloads = _parse_evaluate_result(
                            await page.evaluate(EXTRACT_DATA_SJS_JS)
                        ) or []
                    if sjs_payloads:
                        sjs_detail = parse_data_sjs_payloads(sjs_payloads)
                        if sjs_detail != DetailPageData():
                            detail = sjs_detail
                            extraction_tier = "data_sjs"
                            break

                    # If no data-sjs match, search ALL script tags for listing data
                    # (Facebook may use different script types on authenticated pages)
                    if _attempt == 0:
                        diag = _parse_evaluate_result(await page.evaluate("""() => {
                            const result = {total_scripts: 0, sjs_count: 0, json_count: 0,
                                            has_marketplace: false, marketplace_snippet: ''};
                            const allScripts = document.querySelectorAll('script');
                            result.total_scripts = allScripts.length;
                            const sjsScripts = document.querySelectorAll(
                                'script[type="application/json"][data-sjs]');
                            result.sjs_count = sjsScripts.length;
                            const jsonScripts = document.querySelectorAll(
                                'script[type="application/json"]');
                            result.json_count = jsonScripts.length;
                            // Search ALL scripts for marketplace keywords
                            for (const s of allScripts) {
                                const t = s.textContent || '';
                                if (t.includes('marketplace_listing_title')
                                    || t.includes('redacted_description')) {
                                    result.has_marketplace = true;
                                    const idx = t.indexOf('marketplace_listing_title');
                                    if (idx >= 0) {
                                        result.marketplace_snippet = t.substring(
                                            Math.max(0, idx - 20), idx + 80);
                                    }
                                    break;
                                }
                            }
                            // Also check page HTML directly
                            const html = document.documentElement.innerHTML;
                            if (!result.has_marketplace
                                && html.includes('marketplace_listing_title')) {
                                result.has_marketplace = true;
                                const idx = html.indexOf('marketplace_listing_title');
                                result.marketplace_snippet = 'HTML:' + html.substring(
                                    Math.max(0, idx - 20), idx + 80);
                            }
                            return result;
                        }"""))
                        if diag and isinstance(diag, dict):
                            log.debug(
                                "detail_extractor.page_diagnostic",
                                external_id=listing.external_id,
                                total_scripts=diag.get("total_scripts", 0),
                                sjs_count=diag.get("sjs_count", 0),
                                json_count=diag.get("json_count", 0),
                                has_marketplace=diag.get("has_marketplace", False),
                                snippet=diag.get("marketplace_snippet", "")[:100],
                            )
                            # If marketplace data IS in the HTML but not in data-sjs,
                            # extract it from the full page HTML
                            if diag.get("has_marketplace"):
                                all_json = _parse_evaluate_result(
                                    await page.evaluate("""() => {
                                    const results = [];
                                    const scripts = document.querySelectorAll(
                                        'script[type="application/json"]');
                                    for (const s of scripts) {
                                        const t = s.textContent || '';
                                        if (t.includes('marketplace_listing_title')
                                            || t.includes('listing_price')
                                            || t.includes('redacted_description')) {
                                            results.push(t);
                                        }
                                    }
                                    return results;
                                }""")) or []
                                if all_json:
                                    sjs_detail = parse_data_sjs_payloads(all_json)
                                    if sjs_detail != DetailPageData():
                                        detail = sjs_detail
                                        extraction_tier = "json_script"
                                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)

        # Tier 2: DOM structural selectors (fill gaps from Tier 1)
        try:
            dom_raw = _parse_evaluate_result(
                await page.evaluate(EXTRACT_DOM_DETAIL_JS)
            ) or {}
            if isinstance(dom_raw, dict):
                dom_detail = parse_dom_detail(dom_raw)
                # Fill any fields that Tier 1 missed
                if not detail.title and dom_detail.title:
                    detail.title = dom_detail.title
                if not detail.description and dom_detail.description:
                    detail.description = dom_detail.description
                if detail.price is None and dom_detail.price is not None:
                    detail.price = dom_detail.price
                if not detail.location and dom_detail.location:
                    detail.location = dom_detail.location
                if not detail.seller_name and dom_detail.seller_name:
                    detail.seller_name = dom_detail.seller_name
                if not detail.condition and dom_detail.condition:
                    detail.condition = dom_detail.condition
                if detail.posted_at is None and dom_detail.posted_at is not None:
                    detail.posted_at = dom_detail.posted_at
                if not detail.image_urls and dom_detail.image_urls:
                    detail.image_urls = dom_detail.image_urls
        except Exception as exc:
            log.debug("DOM extraction failed", error=str(exc)[:100])

        # Tier 3: Page markdown for freshness + seller (last resort)
        if detail.posted_at is None or not detail.seller_name:
            try:
                page_text = ""
                try:
                    page_text, _ = await page._extract_clean_markdown()
                except Exception:
                    pass

                if page_text and detail.posted_at is None:
                    import re
                    from agentic_scraper.sites.facebook.parser import _parse_freshness

                    fresh_match = re.search(
                        r"(Just listed|Listed (?:\d+ (?:minutes?|hours?|days?|weeks?) ago"
                        r"|yesterday|last week))",
                        page_text, re.IGNORECASE,
                    )
                    if fresh_match:
                        detail.posted_at = _parse_freshness(fresh_match.group(0))

                if page_text and not detail.seller_name:
                    import re
                    seller_match = re.search(
                        r"Seller information\s*\n+\s*(.+?)(?:\n|$)", page_text,
                    )
                    if seller_match:
                        detail.seller_name = seller_match.group(1).strip()
            except Exception:
                pass

        # Validate: if enrichment found a DIFFERENT listing (Facebook redirected
        # to a "similar item" when the original was sold/deleted), discard
        # the enriched data to avoid cross-contamination.
        # Only check when the original title is substantial (>= 5 words) —
        # short/vague titles like "FREE" or "Just listed" should be overridden.
        orig_title_clean = (listing.title or "").strip().lower()
        orig_words = set(orig_title_clean.split())
        if (
            detail.title
            and listing.title
            and len(orig_words) >= 3
            and detail.title.lower() != orig_title_clean
        ):
            detail_words = set(detail.title.lower().split())
            overlap = orig_words & detail_words
            max_words = max(len(orig_words), 1)
            if len(overlap) / max_words < 0.3:
                log.warning(
                    "Enrichment discarded — likely different listing (redirect)",
                    external_id=listing.external_id,
                    original_title=listing.title[:50],
                    enriched_title=detail.title[:50],
                    overlap_pct=round(len(overlap) / max_words * 100),
                )
                detail = DetailPageData(
                    posted_at=detail.posted_at,
                    condition=detail.condition,
                )
                extraction_tier = "none"

        # Build enriched listing — never overwrite existing good data
        title = detail.title or listing.title
        description = detail.description or listing.description
        price = listing.price if listing.price is not None else detail.price
        posted_at = listing.posted_at or detail.posted_at
        location = detail.location or listing.location
        seller_name = detail.seller_name or listing.seller_name

        # Merge images: keep original search thumbnail first (most reliable),
        # then append detail page images for additional context.
        # Cap at 5 new images — data-sjs can pick up images from related
        # listings on the same page (search feed behind the detail overlay).
        image_urls = list(listing.image_urls)
        if detail.image_urls:
            added = 0
            for url in detail.image_urls:
                if url not in image_urls:
                    image_urls.append(url)
                    added += 1
                    if added >= 5:
                        break

        # Store structured data in raw_data for downstream access
        raw_data = dict(listing.raw_data)
        if detail.condition:
            raw_data["condition"] = detail.condition
        if detail.is_sold:
            raw_data["is_sold"] = True
        if detail.is_pending:
            raw_data["is_pending"] = True
        if detail.strikethrough_price is not None:
            raw_data["strikethrough_price"] = detail.strikethrough_price
        if detail.latitude is not None:
            raw_data["latitude"] = detail.latitude
            raw_data["longitude"] = detail.longitude
        if extraction_tier == "none" and any(
            [title != listing.title, description != listing.description]
        ):
            extraction_tier = "dom"
        raw_data["detail_extraction_tier"] = extraction_tier

        enriched = replace(
            listing,
            title=title,
            description=description,
            image_urls=image_urls,
            price=price,
            location=location,
            seller_name=seller_name,
            posted_at=posted_at,
            raw_data=raw_data,
        )

        log.debug(
            "Detail extraction complete",
            external_id=listing.external_id,
            title=title[:50] if title else "",
            tier=raw_data.get("detail_extraction_tier", "none"),
            has_desc=bool(description),
            desc_len=len(description or ""),
            has_price=price is not None,
            has_posted=posted_at is not None,
        )
        return enriched

    except Exception as exc:
        log.warning(
            "Detail extraction failed, keeping existing data",
            external_id=listing.external_id,
            error=str(exc),
        )
        return listing
