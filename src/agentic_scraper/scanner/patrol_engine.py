"""PatrolEngine - orchestrates the 4-phase patrol cycle.

GraphQL-first pipeline: intercept network responses for rich listing data,
fall back to DOM extraction when GraphQL yields nothing.
Batch dedup replaces N individual exists() calls.
Deep inspection eliminated — GraphQL provides full data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentic_scraper.browser.graphql_interceptor import GraphQLInterceptor, GraphQLListingData
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
class PatrolCycleResult:
    """Summary of a single patrol cycle."""

    categories_swept: int = 0
    total_listings_seen: int = 0
    new_listings: int = 0
    deep_inspected: int = 0
    deals_found: int = 0
    deals_notified: int = 0
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
        raw_data=raw_data,
    )


class PatrolEngine:
    """Orchestrates the 4-phase patrol cycle.

    Phase 1: Sweep + Intercept (navigate, scroll, capture GraphQL or DOM)
    Phase 2: Batch Dedup (single query to filter already-seen listings)
    Phase 3: Evaluation (SmartDealRadar + InterestMatcher on new listings)
    Phase 4: Notify (DealNotifier for qualifying deals)

    Deep inspection is eliminated — GraphQL data includes descriptions,
    images, condition, and timestamps. DOM fallback provides surface data
    only when GraphQL intercept fails.

    Args:
        browser_manager: Browser lifecycle manager with get_page()/get_session().
        listing_repo: Listing persistence.
        watchlist_repo: Watch item persistence (for interest matching).
        deal_repo: Deal persistence.
        scan_log_repo: Scan log persistence.
        notifier: Discord deal notifier.
        interest_matcher: InterestMatcher for matching against user interests.
        smart_deal_radar: SmartDealRadar v2 for skill-based deal evaluation.
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

        # GraphQL interceptor for network-level data capture
        self._interceptor = GraphQLInterceptor()

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

            # Phase 1: Sweep + Intercept (GraphQL primary, DOM fallback)
            all_listings, data_source = await self._sweep_and_intercept(page, result)
            result.data_source = data_source

            # Phase 2: Batch Dedup
            new_listings = await self._batch_dedup_and_save(all_listings, result)

            # Phase 3: Evaluation (no deep inspection needed)
            deals = await self._evaluate(new_listings, result)

            # Phase 4: Notify
            await self._notify(deals, new_listings, result)

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

    async def _sweep_and_intercept(
        self, page: object, result: PatrolCycleResult
    ) -> tuple[list[Listing], str]:
        """Phase 1: Navigate + scroll, try GraphQL intercept, fall back to DOM.

        Returns:
            Tuple of (listings, data_source).
        """
        # Try to start GraphQL interceptor if browser session available
        interceptor_active = False
        try:
            session = self._browser.get_session()
            await self._interceptor.start(session)
            interceptor_active = True
        except Exception as exc:
            log.debug("GraphQL interceptor start failed, using DOM only", error=str(exc))

        # Perform the actual sweep (navigate + scroll + DOM extract)
        dom_listings = await self._sweep_categories(page, result)

        # Try to drain GraphQL data
        graphql_listings: list[Listing] = []
        if interceptor_active:
            try:
                gql_data = self._interceptor.drain()
                graphql_listings = [_graphql_to_listing(g) for g in gql_data]
                log.info(
                    "GraphQL intercept results",
                    graphql_count=len(graphql_listings),
                    dom_count=len(dom_listings),
                )
            except Exception as exc:
                log.warning("GraphQL drain failed", error=str(exc))
            finally:
                try:
                    await self._interceptor.stop()
                except Exception:
                    pass

        # Decide which data source to use
        if graphql_listings:
            # GraphQL data is richer — prefer it
            if dom_listings and not graphql_listings:
                return dom_listings, "dom_fallback"

            # Merge: GraphQL is primary, fill gaps with DOM
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

    async def _batch_dedup_and_save(
        self, listings: list[Listing], result: PatrolCycleResult
    ) -> list[Listing]:
        """Phase 2: Batch dedup using single SQL query, then save new listings."""
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

    async def _evaluate(
        self, listings: list[Listing], result: PatrolCycleResult
    ) -> list[Deal]:
        """Phase 3: Run SmartDealRadar + InterestMatcher on new listings."""
        deals: list[Deal] = []
        interests = await self._watchlist_repo.list_active()

        # Limit evaluations per cycle
        to_evaluate = listings[: self._max_evaluations]

        for listing in to_evaluate:
            deal = None

            # Try SmartDealRadar first
            if self._smart_deal_radar:
                try:
                    deal = await self._smart_deal_radar.evaluate(listing)
                except Exception as exc:
                    log.warning(
                        "SmartDealRadar evaluation failed",
                        listing_id=listing.id,
                        error=str(exc),
                    )

            # If radar found a deal, use it
            if deal is not None:
                deals.append(deal)
                continue

            # Otherwise check interest matches
            if interests:
                interest_deals = self._interest_matcher.match_single(listing, interests)
                if interest_deals:
                    deals.append(interest_deals[0])

        result.deals_found = len(deals)

        log.info(
            "Evaluation complete",
            evaluated=len(to_evaluate),
            deals_found=len(deals),
        )
        return deals

    async def _notify(
        self,
        deals: list[Deal],
        listings: list[Listing],
        result: PatrolCycleResult,
    ) -> None:
        """Phase 4: Save deals and send Discord notifications."""
        listing_map = {l.id: l for l in listings if l.id}

        for deal in deals:
            # Check minimum score threshold
            if _SCORE_RANK.get(deal.score, 0) < _SCORE_RANK.get(self._min_score, 0):
                continue

            try:
                await self._deal_repo.save(deal)
                listing = listing_map.get(deal.listing_id)
                if listing:
                    await self._notifier.send_deal(deal, listing)
                    result.deals_notified += 1
            except Exception as exc:
                log.error(
                    "Deal notification failed",
                    deal_listing_id=deal.listing_id,
                    error=str(exc),
                )
