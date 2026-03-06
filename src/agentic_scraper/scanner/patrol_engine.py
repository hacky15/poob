"""PatrolEngine - orchestrates the patrol cycle.

GraphQL-first pipeline: intercept network responses for rich listing data,
fall back to DOM extraction when GraphQL yields nothing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from agentic_scraper.browser.graphql_interceptor import (
    GraphQLListingData,
    parse_graphql_listings,
)
from agentic_scraper.browser.stealth import random_delay
from agentic_scraper.scanner.interest_matcher import InterestMatcher
from agentic_scraper.sites.facebook.patrol_scanner import PatrolScanner, RadiusOscillator
from agentic_scraper.storage.models import Deal, DealScore, Listing, ScanLog
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.skills.orchestrator import SmartDealRadar
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.exclusion_repo import ExclusionRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
    from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

log = get_logger("scanner.patrol_engine")

# Map string score names to DealScore for config-based filtering
_SCORE_RANK: dict[DealScore, int] = {
    DealScore.UNKNOWN: 0,
    DealScore.FAIR: 1,
    DealScore.GOOD: 2,
    DealScore.GREAT: 3,
    DealScore.INCREDIBLE: 4,
}


@dataclass
class EvaluationResult:
    """Structured output from deal evaluation."""

    base_deals: list[Deal] = field(default_factory=list)
    watchlist_deals: list[Deal] = field(default_factory=list)


@dataclass
class PatrolCycleResult:
    """Summary of a single patrol cycle."""

    categories_swept: int = 0
    total_listings_seen: int = 0
    new_listings: int = 0
    deep_inspected: int = 0
    deals_found: int = 0
    deals_notified: int = 0
    sponsored_filtered: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    data_source: str = ""  # "graphql", "dom_fallback", or "mixed"
    suspected_shadow_ban: bool = False


def _graphql_to_listing(gql: GraphQLListingData) -> Listing:
    """Convert a GraphQLListingData into a Listing model."""
    raw_data: dict = {}
    if gql.condition:
        raw_data["condition"] = gql.condition
    if gql.posted_at:
        raw_data["posted_at_raw"] = gql.posted_at

    # Parse creation_time unix timestamp into proper datetime
    posted_at = None
    if gql.posted_at:
        try:
            posted_at = datetime.fromtimestamp(int(gql.posted_at), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            pass

    return Listing(
        site="facebook_marketplace",
        external_id=gql.external_id,
        title=gql.title,
        price=gql.price,
        location=gql.location,
        listing_url=gql.listing_url,
        image_urls=gql.image_urls,
        description=gql.description,
        seller_name=gql.seller_name,
        posted_at=posted_at,
        raw_data=raw_data,
        is_sponsored=gql.is_sponsored,
    )


class PatrolEngine:
    """Orchestrates the patrol cycle.

    1. Sweep + Intercept: navigate, scroll, capture GraphQL or DOM data
    2. Batch Dedup: single query to filter already-seen listings
    3. Evaluate: SmartDealRadar + InterestMatcher on new listings
    4. Notify: DealNotifier for qualifying deals

    Args:
        browser_manager: Browser lifecycle manager.
        listing_repo: Listing persistence.
        watchlist_repo: Watch item persistence (for interest matching).
        deal_repo: Deal persistence.
        scan_log_repo: Scan log persistence.
        notifier: Discord deal notifier.
        interest_matcher: InterestMatcher for matching against user interests.
        smart_deal_radar: SmartDealRadar for deal evaluation.
        config: Application configuration.
    """

    def __init__(
        self,
        *,
        browser_manager: object,
        listing_repo: ListingRepository,
        watchlist_repo: WatchlistRepository,
        deal_repo: DealRepository,
        scan_log_repo: ScanLogRepository,
        notifier: DealNotifier,
        interest_matcher: InterestMatcher,
        smart_deal_radar: SmartDealRadar | None = None,
        exclusion_repo: ExclusionRepository | None = None,
        config: AppConfig,
    ) -> None:
        self._browser = browser_manager
        self._listing_repo = listing_repo
        self._watchlist_repo = watchlist_repo
        self._deal_repo = deal_repo
        self._scan_log_repo = scan_log_repo
        self._notifier = notifier
        self._interest_matcher = interest_matcher
        self._smart_deal_radar = smart_deal_radar
        self._exclusion_repo = exclusion_repo
        self._config = config

        # Build patrol scanner — use fixed radius when oscillation disabled
        fixed_radius: int | None = None
        if not getattr(config, "patrol_radius_oscillation_enabled", False):
            fixed_radius = config.patrol_base_radius_miles

        oscillator = RadiusOscillator(
            base_radius=config.patrol_base_radius_miles,
            jitter=config.patrol_radius_jitter,
        )
        self._scanner = PatrolScanner(
            radius_oscillator=oscillator,
            scroll_steps=5,
            days_since_listed=config.patrol_days_since_listed,
            fixed_radius=fixed_radius,
        )

        # Parse min score from config string
        self._min_score = DealScore(config.deal_radar_min_score)
        self._max_evaluations = config.deal_radar_max_evaluations

        # Sweep mode: "unified" (1 page load) or "categories" (multi-page)
        self._sweep_mode = getattr(config, "patrol_sweep_mode", "unified")

        # Shadow ban tracking
        self._consecutive_empty_sweeps = 0

    async def run_patrol_cycle(self) -> PatrolCycleResult:
        """Execute one full 4-phase patrol cycle.

        Returns:
            PatrolCycleResult with cycle statistics.
        """
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc)
        result = PatrolCycleResult()

        log.info("Starting patrol cycle", sweep_mode=self._sweep_mode)

        try:
            page = await self._browser.get_page()

            # Step 1: Sweep + intercept (GraphQL primary, DOM fallback)
            all_listings, data_source = await self._sweep_and_intercept(page, result)
            result.data_source = data_source

            # Step 1b: Watchlist keyword searches
            if getattr(self._config, "patrol_watchlist_sweep_enabled", True):
                watchlist_listings = await self._sweep_watchlist_items(page, result)
                # Merge; dedup handles overlap below
                existing_ids = {l.external_id for l in all_listings if l.external_id}
                for wl in watchlist_listings:
                    if wl.external_id and wl.external_id not in existing_ids:
                        all_listings.append(wl)
                        existing_ids.add(wl.external_id)

            # Step 2: Batch dedup
            new_listings = await self._batch_dedup_and_save(all_listings, result)

            # Step 2b: Exempt watchlist matches from freshness filter.
            # DOM-sourced watchlist results lack posted_at timestamps.
            exempt_ids: set[str] = set()
            interests = await self._watchlist_repo.list_active()
            if interests:
                for listing in new_listings:
                    if listing.posted_at is None:
                        matches = self._interest_matcher.match_single(
                            listing, interests
                        )
                        if matches:
                            exempt_ids.add(listing.external_id)
                if exempt_ids:
                    log.info(
                        "Watchlist pre-filter exemptions",
                        exempt_count=len(exempt_ids),
                    )

            # Step 2c: Filter stale + sponsored listings (watchlist matches exempt)
            sponsored_pre = sum(1 for l in new_listings if l.is_sponsored)
            new_listings = self._filter_stale(new_listings, exempt_ids=exempt_ids)
            result.sponsored_filtered = sponsored_pre

            # Step 2d: Sort order verification (diagnostic only)
            self._verify_sort_order(new_listings)

            # Step 3: Evaluate (SmartDealRadar + InterestMatcher)
            eval_result = await self._evaluate(new_listings, result)

            # Step 4: Notify (public channel + user DMs)
            await self._notify(eval_result, new_listings, result)

        except Exception as exc:
            log.error("Patrol cycle failed", error=str(exc))
            result.errors.append(str(exc))

        result.duration_seconds = time.monotonic() - start_time

        # Log the cycle
        scan_log = ScanLog(
            site="facebook_marketplace",
            category="patrol",
            listings_found=result.new_listings,
            deals_found=result.deals_found,
            errors=result.errors,
            duration_seconds=result.duration_seconds,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )
        await self._scan_log_repo.save(scan_log)

        log.info(
            "Patrol cycle complete",
            categories=result.categories_swept,
            total_seen=result.total_listings_seen,
            new=result.new_listings,
            deals=result.deals_found,
            notified=result.deals_notified,
            source=result.data_source,
            shadow_ban=result.suspected_shadow_ban,
            duration=f"{result.duration_seconds:.1f}s",
        )

        return result

    # JavaScript that wraps both window.fetch AND XMLHttpRequest to capture
    # GraphQL responses. Facebook uses XHR (not fetch) for /api/graphql calls.
    # Injected before navigation; captured data read after scrolling.
    _INJECT_GQL_CAPTURE_JS = """() => {
        if (window.__gql_captures) return;
        window.__gql_captures = [];

        // Wrap fetch (some FB paths may use it)
        const origFetch = window.fetch;
        window.fetch = async function(...args) {
            const response = await origFetch.apply(this, args);
            const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
            if (url.includes('/api/graphql')) {
                try {
                    const clone = response.clone();
                    const text = await clone.text();
                    window.__gql_captures.push(text);
                } catch (e) {}
            }
            return response;
        };

        // Wrap XMLHttpRequest (Facebook's primary transport for GraphQL)
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;

        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
            this.__gqlUrl = url;
            return origOpen.call(this, method, url, ...rest);
        };

        XMLHttpRequest.prototype.send = function(body) {
            if (this.__gqlUrl && this.__gqlUrl.includes('/api/graphql')) {
                this.addEventListener('load', function() {
                    try {
                        if (this.responseText) {
                            window.__gql_captures.push(this.responseText);
                        }
                    } catch (e) {}
                });
            }
            return origSend.call(this, body);
        };
    }"""

    _DRAIN_GQL_CAPTURE_JS = """() => {
        const captures = window.__gql_captures || [];
        window.__gql_captures = [];
        return captures;
    }"""

    async def _sweep_and_intercept(
        self, page: object, result: PatrolCycleResult
    ) -> tuple[list[Listing], str]:
        """Navigate + scroll, capture GraphQL via JS interception, fall back to DOM.

        Injects a fetch wrapper before navigation that captures Facebook's
        GraphQL responses, providing creation_time for freshness filtering.

        Returns:
            Tuple of (listings, data_source).
        """
        # Inject GraphQL fetch interceptor before navigation
        try:
            await page.evaluate(self._INJECT_GQL_CAPTURE_JS)
        except Exception as exc:
            log.debug("GraphQL JS interceptor injection failed", error=str(exc))

        # Perform the actual sweep (navigate + scroll + DOM extract)
        # Navigation and scrolling trigger GraphQL requests that we capture
        dom_listings = await self._sweep_categories(page, result)

        # Re-inject after navigation (page.goto resets the document)
        try:
            await page.evaluate(self._INJECT_GQL_CAPTURE_JS)
        except Exception:
            pass

        # Drain captured GraphQL response bodies from JavaScript
        captured_bodies: list[str] = []
        try:
            raw = await page.evaluate(self._DRAIN_GQL_CAPTURE_JS)
            if isinstance(raw, list):
                captured_bodies = [b for b in raw if isinstance(b, str) and b]
        except Exception as exc:
            log.debug("GraphQL JS drain failed", error=str(exc))

        # Parse captured GraphQL responses into listings
        graphql_listings: list[Listing] = []
        seen_gql_ids: set[str] = set()
        for body in captured_bodies:
            for gql in parse_graphql_listings(body):
                if gql.external_id and gql.external_id not in seen_gql_ids:
                    seen_gql_ids.add(gql.external_id)
                    graphql_listings.append(_graphql_to_listing(gql))

        log.info(
            "GraphQL intercept results",
            graphql_responses=len(captured_bodies),
            graphql_count=len(graphql_listings),
            dom_count=len(dom_listings),
        )

        # Decide which data source to use
        if graphql_listings:
            # GraphQL data is richer — prefer it, fill gaps with DOM
            gql_ids = {l.external_id for l in graphql_listings}
            for dl in dom_listings:
                if dl.external_id and dl.external_id not in gql_ids:
                    graphql_listings.append(dl)
                    gql_ids.add(dl.external_id)

            data_source = "graphql" if len(gql_ids) > len(dom_listings) else "mixed"
            self._consecutive_empty_sweeps = 0
            return graphql_listings, data_source

        # GraphQL empty — fall back to DOM
        if dom_listings:
            self._consecutive_empty_sweeps = 0
            return dom_listings, "dom_fallback"

        # Both empty — possible shadow ban
        self._consecutive_empty_sweeps += 1
        if self._consecutive_empty_sweeps >= 3:
            result.suspected_shadow_ban = True
            log.warning(
                "Possible shadow ban detected",
                consecutive_empty=self._consecutive_empty_sweeps,
            )

        return [], "dom_fallback"

    async def _sweep_categories(
        self, page: object, result: PatrolCycleResult
    ) -> list[Listing]:
        """Sweep categories via DOM extraction (also triggers GraphQL requests)."""
        all_listings: list[Listing] = []
        seen_external_ids: set[str] = set()

        if self._sweep_mode == "unified":
            # Single unified feed — 1 page load captures all new local listings
            categories: list[str | None] = [None]
        else:
            # Legacy category sweep
            categories = list(self._config.patrol_categories)
            if self._config.patrol_include_all_categories:
                categories.append(None)

        for category in categories:
            try:
                listings = await self._scanner.sweep_category(page, category)

                for listing in listings:
                    if listing.external_id not in seen_external_ids:
                        seen_external_ids.add(listing.external_id)
                        all_listings.append(listing)

                result.categories_swept += 1
                log.debug(
                    "Category swept",
                    category=category or "all",
                    found=len(listings),
                )
            except Exception as exc:
                log.warning(
                    "Category sweep failed",
                    category=category or "all",
                    error=str(exc),
                )
                result.errors.append(f"Sweep {category or 'all'}: {exc}")

            # Delay between categories (stealth) — only for multi-category mode
            if len(categories) > 1 and category is not categories[-1]:
                await random_delay(
                    self._config.patrol_inter_category_delay_min_ms,
                    self._config.patrol_inter_category_delay_max_ms,
                )

        result.total_listings_seen = len(all_listings)
        return all_listings

    async def _sweep_watchlist_items(
        self, page: object, result: PatrolCycleResult
    ) -> list[Listing]:
        """Search Facebook for each active watchlist item.

        Runs a keyword search per watchlist interest, collecting fresh listings
        that the main category sweep might have missed.

        Args:
            page: Browser page instance.
            result: Patrol cycle result to update.

        Returns:
            Combined list of listings from all watchlist searches.
        """
        interests = await self._watchlist_repo.list_active()
        if not interests:
            return []

        max_items = getattr(self._config, "patrol_watchlist_max_items", 10)
        interests = interests[:max_items]

        all_search_listings: list[Listing] = []
        seen_ids: set[str] = set()

        searches_run = 0
        for item in interests:
            # Build list of search configs to run for this item.
            # If search_configs is empty, fall back to a single default search.
            configs = item.search_configs if item.search_configs else [{}]

            for cfg in configs:
                try:
                    listings = await self._scanner.sweep_search(
                        page,
                        item.interest,
                        max_price=cfg.get("max_price", item.max_price),
                        min_price=cfg.get("min_price"),
                        location_slug=cfg.get("location"),
                        condition=cfg.get("condition"),
                        radius_miles=cfg.get("radius_miles"),
                    )
                    for listing in listings:
                        if listing.external_id and listing.external_id not in seen_ids:
                            seen_ids.add(listing.external_id)
                            all_search_listings.append(listing)
                    searches_run += 1

                    # Stealth delay between searches
                    await random_delay(
                        self._config.patrol_inter_category_delay_min_ms,
                        self._config.patrol_inter_category_delay_max_ms,
                    )
                except Exception as exc:
                    log.warning(
                        "Watchlist search failed",
                        interest=item.interest,
                        config=cfg,
                        error=str(exc),
                    )

        log.info(
            "Watchlist sweep complete",
            interests_searched=len(interests),
            searches_run=searches_run,
            listings_found=len(all_search_listings),
        )
        return all_search_listings

    async def _batch_dedup_and_save(
        self, listings: list[Listing], result: PatrolCycleResult
    ) -> list[Listing]:
        """Batch dedup using single SQL query, then save new listings."""
        if not listings:
            result.new_listings = 0
            return []

        # Collect external IDs for batch check
        external_ids = [l.external_id for l in listings if l.external_id]

        if external_ids:
            try:
                new_ids = await self._listing_repo.filter_new_ids(
                    "facebook_marketplace", external_ids
                )
            except Exception as exc:
                log.warning("Batch dedup failed, falling back to individual checks", error=str(exc))
                new_ids = None
        else:
            new_ids = None

        new_listings: list[Listing] = []

        if new_ids is not None:
            # Batch dedup succeeded
            for listing in listings:
                if listing.external_id in new_ids:
                    saved = await self._listing_repo.save(listing)
                    new_listings.append(saved)
        else:
            # Fallback: individual exists() checks
            for listing in listings:
                if listing.external_id and await self._listing_repo.exists(
                    listing.site, listing.external_id
                ):
                    continue
                saved = await self._listing_repo.save(listing)
                new_listings.append(saved)

        result.new_listings = len(new_listings)

        log.info(
            "Dedup results",
            total=len(listings),
            new=len(new_listings),
            already_seen=len(listings) - len(new_listings),
            method="batch" if new_ids is not None else "individual",
        )
        return new_listings

    def _filter_stale(
        self,
        listings: list[Listing],
        exempt_ids: set[str] | None = None,
    ) -> list[Listing]:
        """Filter out sponsored, stale, and timestamp-less listings.

        Sponsored listings are ALWAYS discarded — they are paid promotions
        injected by Facebook's engagement algorithm, not organic deals.

        Listings without a posted_at timestamp are kept (DOM scraper can't
        extract dates, but the page is sorted newest-first so they're likely
        fresh). The dedup filter prevents re-evaluation of already-seen IDs.

        Args:
            listings: Listings to filter.
            exempt_ids: External IDs exempt from the no-timestamp discard
                (typically watchlist-matched listings from DOM keyword searches).

        Returns:
            Listings that pass the freshness check.
        """
        max_age_hours = getattr(self._config, "listing_max_age_hours", 6)
        exempt = exempt_ids or set()

        # Always filter sponsored and shipping listings, even when age filter is disabled
        filtered: list[Listing] = []
        sponsored_count = 0
        shipping_count = 0
        for listing in listings:
            if listing.is_sponsored:
                sponsored_count += 1
                log.debug(
                    "Sponsored listing filtered",
                    title=listing.title[:40],
                    external_id=listing.external_id,
                )
                continue
            # Filter "Ships to you" / non-local listings
            loc = (listing.location or "").lower()
            if "ship" in loc and ("you" in loc or "nationwide" in loc):
                shipping_count += 1
                log.debug(
                    "Shipping listing filtered",
                    title=listing.title[:40],
                    location=listing.location,
                )
                continue
            filtered.append(listing)

        if max_age_hours <= 0:
            if sponsored_count or shipping_count:
                log.info(
                    "Freshness filter applied",
                    kept=len(filtered),
                    sponsored=sponsored_count,
                    shipping=shipping_count,
                )
            return filtered

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        fresh: list[Listing] = []
        stale_count = 0
        no_timestamp_count = 0
        exempt_kept = 0

        for listing in filtered:
            if listing.posted_at is None:
                # No timestamp = DOM scraper couldn't extract it.
                # Since we sort by newest-first, these are likely fresh.
                # Keep them — the dedup filter already prevents re-evaluation.
                fresh.append(listing)
                if listing.external_id in exempt:
                    exempt_kept += 1
                else:
                    no_timestamp_count += 1
            elif listing.posted_at >= cutoff:
                fresh.append(listing)
            else:
                stale_count += 1
                log.debug(
                    "Stale listing filtered",
                    title=listing.title[:40],
                    posted_at=str(listing.posted_at),
                )

        if stale_count or no_timestamp_count or exempt_kept or sponsored_count or shipping_count:
            log.info(
                "Freshness filter applied",
                kept=len(fresh),
                stale=stale_count,
                no_timestamp=no_timestamp_count,
                watchlist_exempt=exempt_kept,
                sponsored=sponsored_count,
                shipping=shipping_count,
            )

        return fresh

    def _verify_sort_order(self, listings: list[Listing]) -> None:
        """Verify that listings are in descending posted_at order.

        Facebook claims to sort by creation_time_descend, but the engagement
        algorithm injects promoted and recommended listings. This method checks
        if the returned listings respect the requested sort order and logs a
        warning if they don't.

        Only checks listings that have a posted_at timestamp.
        """
        timestamped = [l for l in listings if l.posted_at is not None]
        if len(timestamped) < 2:
            return

        violations = sum(
            1
            for i in range(len(timestamped) - 1)
            if timestamped[i].posted_at < timestamped[i + 1].posted_at  # type: ignore[operator]
        )
        if violations:
            log.warning(
                "Sort order verification failed",
                total_timestamped=len(timestamped),
                sort_violations=violations,
            )
        else:
            log.debug(
                "Sort order verified",
                total_timestamped=len(timestamped),
            )

    async def _evaluate(
        self, listings: list[Listing], result: PatrolCycleResult
    ) -> EvaluationResult:
        """Run deal evaluation pipeline on new listings.

        SmartDealRadar handles text triage, visual enrichment, comparable
        sales, and VLM evaluation. Watchlist matches are prioritized.
        """
        interests = await self._watchlist_repo.list_active()

        # Prioritize: watchlist-matched listings first, then the rest
        if interests:
            watchlist_matched: list[Listing] = []
            non_matched: list[Listing] = []
            for listing in listings:
                matches = self._interest_matcher.match_single(listing, interests)
                if matches:
                    watchlist_matched.append(listing)
                else:
                    non_matched.append(listing)
            prioritized = watchlist_matched + non_matched
            if watchlist_matched:
                log.info(
                    "Watchlist priority sort",
                    watchlist_first=len(watchlist_matched),
                    other=len(non_matched),
                )
        else:
            prioritized = listings

        # Limit evaluations per cycle
        to_evaluate = prioritized[: self._max_evaluations]

        eval_result = EvaluationResult()

        if not self._smart_deal_radar or not to_evaluate:
            return eval_result

        try:
            pipeline_results = await self._smart_deal_radar.evaluate_batch(
                to_evaluate, watchlist_items=interests
            )
        except Exception as exc:
            log.error("Pipeline evaluation failed", error=str(exc))
            result.errors.append(f"Pipeline: {exc}")
            return eval_result

        for deal, vlm_eval in pipeline_results:
            if deal is None:
                continue
            if deal.watch_item_id:
                eval_result.watchlist_deals.append(deal)
            else:
                eval_result.base_deals.append(deal)

        total = len(eval_result.base_deals) + len(eval_result.watchlist_deals)
        result.deals_found = total

        log.info(
            "Evaluation complete",
            evaluated=len(to_evaluate),
            base_deals=len(eval_result.base_deals),
            watchlist_deals=len(eval_result.watchlist_deals),
        )
        return eval_result

    async def _load_exclusion_keywords(self) -> dict[str, set[str]]:
        """Load all active exclusion keywords grouped by user ID.

        Returns:
            Mapping of discord_user_id → set of lowercased excluded keywords.
            Also includes a "__global__" key with ALL keywords for public channel filtering.
        """
        if not self._exclusion_repo:
            return {}
        try:
            all_items = await self._exclusion_repo.list_all_active()
            by_user: dict[str, set[str]] = {"__global__": set()}
            for item in all_items:
                kw = item.keyword.lower()
                by_user.setdefault(item.discord_user_id, set()).add(kw)
                by_user["__global__"].add(kw)
            return by_user
        except Exception as exc:
            log.warning("Failed to load exclusion keywords", error=str(exc))
            return {}

    @staticmethod
    def _is_excluded(listing: Listing, excluded_keywords: set[str]) -> bool:
        """Check if a listing matches any excluded keywords."""
        if not excluded_keywords:
            return False
        text = f"{listing.title} {listing.description}".lower()
        return any(kw in text for kw in excluded_keywords)

    async def _notify(
        self,
        eval_result: EvaluationResult,
        listings: list[Listing],
        result: PatrolCycleResult,
    ) -> None:
        """Route deals to public channel or user DMs.

        Public channel: only deals meeting deal_public_min_score (INCREDIBLE).
        Watchlist DMs: deals meeting deal_watchlist_min_score (GOOD+),
        sent to the Discord user who owns the matching watch item.
        Excludes listings matching user exclusion keywords.
        """
        listing_map = {l.id: l for l in listings if l.id}
        exclusions = await self._load_exclusion_keywords()

        public_min = DealScore(
            getattr(self._config, "deal_public_min_score", "incredible")
        )
        watchlist_min = DealScore(
            getattr(self._config, "deal_watchlist_min_score", "good")
        )

        # Public channel: non-watchlist deals at INCREDIBLE threshold
        global_excluded = exclusions.get("__global__", set())
        for deal in eval_result.base_deals:
            if _SCORE_RANK.get(deal.score, 0) < _SCORE_RANK.get(public_min, 0):
                continue
            listing = listing_map.get(deal.listing_id)
            if listing and self._is_excluded(listing, global_excluded):
                log.debug("Deal excluded by keyword", title=listing.title[:40])
                continue
            try:
                await self._deal_repo.save(deal)
                if listing:
                    await self._notifier.send_deal(deal, listing)
                    result.deals_notified += 1
            except Exception as exc:
                log.error(
                    "Public deal notification failed",
                    deal_listing_id=deal.listing_id,
                    error=str(exc),
                )

        # Watchlist DMs: per-user notification_threshold
        threshold_rank = {"fair": 1, "good": 2, "great": 3, "incredible": 4}
        for deal in eval_result.watchlist_deals:
            listing = listing_map.get(deal.listing_id)
            if not listing or not deal.watch_item_id:
                continue
            try:
                watch_item = await self._watchlist_repo.get(deal.watch_item_id)
                if not watch_item or not watch_item.discord_user_id:
                    continue

                # Check user exclusion list
                user_excluded = exclusions.get(watch_item.discord_user_id, set())
                if listing and self._is_excluded(listing, user_excluded):
                    log.debug(
                        "Watchlist deal excluded by keyword",
                        title=listing.title[:40],
                        user_id=watch_item.discord_user_id,
                    )
                    continue

                # Per-user notification threshold
                user_threshold = watch_item.notification_threshold or "good"

                if user_threshold == "free":
                    # Only notify for free listings ($0)
                    if listing.price and listing.price > 0:
                        continue
                elif user_threshold == "all":
                    pass  # Notify for everything that matched
                else:
                    # Check deal quality against user's threshold
                    deal_rank = threshold_rank.get(deal.score.value, 0)
                    min_rank = threshold_rank.get(user_threshold, 2)
                    if deal_rank < min_rank:
                        continue

                await self._deal_repo.save(deal)
                await self._notifier.send_deal_dm(
                    deal,
                    listing,
                    discord_user_id=int(watch_item.discord_user_id),
                    watch_interest=watch_item.interest,
                )
                result.deals_notified += 1
            except Exception as exc:
                log.error(
                    "Watchlist DM notification failed",
                    deal_listing_id=deal.listing_id,
                    watch_item_id=deal.watch_item_id,
                    error=str(exc),
                )
