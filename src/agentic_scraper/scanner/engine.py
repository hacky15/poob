"""ScanEngine - orchestrates the full scan cycle."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentic_scraper.scanner.watchlist import WatchlistMatcher
from agentic_scraper.sites.base import ScanQuery
from agentic_scraper.storage.models import Listing, ScanLog
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.skills.orchestrator import SmartDealRadar
    from agentic_scraper.sites.registry import SiteRegistry
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
    from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

log = get_logger("scanner.engine")


class ScanEngine:
    """Orchestrates the full scan cycle: query -> scrape -> dedup -> match -> notify.

    This is the heart of the application. Each scan cycle:
    1. Reads active watch items
    2. Builds queries from watches
    3. Runs each query against each site adapter
    4. Deduplicates against existing listings
    5. Matches new listings against watch items
    6. Runs SmartDealRadar on unmatched listings (v2) for autonomous deal detection
    7. Saves deals and notifies via Discord
    8. Logs the scan

    Args:
        registry: Site adapter registry.
        browser_manager: Browser lifecycle manager.
        llm_provider: LLM provider for agent tasks.
        matcher: WatchlistMatcher for keyword/price matching.
        listing_repo: Listing persistence.
        watchlist_repo: Watch item persistence.
        deal_repo: Deal persistence.
        scan_log_repo: Scan log persistence.
        notifier: Discord deal notifier.
        smart_deal_radar: Optional SmartDealRadar for skill-based deal evaluation.
        deal_radar_max_evaluations: Max listings to evaluate per scan cycle.
    """

    def __init__(
        self,
        *,
        registry: SiteRegistry,
        browser_manager: object,
        llm_provider: object,
        matcher: WatchlistMatcher,
        listing_repo: ListingRepository,
        watchlist_repo: WatchlistRepository,
        deal_repo: DealRepository,
        scan_log_repo: ScanLogRepository,
        notifier: DealNotifier,
        smart_deal_radar: SmartDealRadar | None = None,
        deal_radar_max_evaluations: int = 10,
        browse_enabled: bool = False,
    ) -> None:
        self._registry = registry
        self._browser = browser_manager
        self._llm = llm_provider
        self._matcher = matcher
        self._listing_repo = listing_repo
        self._watchlist_repo = watchlist_repo
        self._deal_repo = deal_repo
        self._scan_log_repo = scan_log_repo
        self._notifier = notifier
        self._smart_deal_radar = smart_deal_radar
        self._deal_radar_max_evaluations = deal_radar_max_evaluations
        self._browse_enabled = browse_enabled

    async def run_scan_cycle(self) -> None:
        """Execute one full scan cycle across all sites and queries.

        Collects all new listings first, then evaluates them for deals
        with a single per-cycle budget (not per-query).
        """
        log.info("Starting scan cycle")

        watch_items = await self._watchlist_repo.list_active()
        if not watch_items and not self._browse_enabled:
            log.info("No active watch items, skipping scan")
            return

        queries = self._matcher.build_queries(watch_items) if watch_items else []

        # Append a browse query to scan the latest local listings
        if self._browse_enabled:
            queries.append(ScanQuery(keywords=""))
            log.info("Browse query appended to scan cycle")

        # Phase 1: Scrape all queries and collect new listings
        all_new_listings: list[Listing] = []
        for site_name in self._registry.list_sites():
            adapter = self._registry.get(site_name)
            if adapter is None:
                continue

            for query in queries:
                new_listings = await self._scrape_query(adapter, query)
                all_new_listings.extend(new_listings)

        log.info("Scrape phase complete", total_new=len(all_new_listings))

        # Phase 2: Evaluate deals with a per-cycle budget
        if all_new_listings:
            if self._smart_deal_radar:
                deals = await self._run_deal_radar(all_new_listings)
            else:
                deals = self._matcher.match(all_new_listings, watch_items)

            for deal in deals:
                await self._deal_repo.save(deal)
                listing = next(
                    (l for l in all_new_listings if l.id == deal.listing_id),
                    None,
                )
                if listing:
                    await self._notifier.send_deal(deal, listing)

            log.info("Deal evaluation complete", deals_found=len(deals))

        log.info("Scan cycle complete")

    async def _scrape_query(
        self, adapter: object, query: object
    ) -> list[Listing]:
        """Scrape a single query and return new (deduped) listings.

        Args:
            adapter: The site adapter to use.
            query: The scan query to execute.

        Returns:
            List of newly-seen listings from this query.
        """
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc)
        errors: list[str] = []

        try:
            llm = self._llm
            result = await adapter.scan(query, self._browser, llm)
            new_listings = await self._dedup_and_save(result.listings)
            errors.extend(result.errors)
        except Exception as exc:
            log.error(
                "Scan failed",
                site=adapter.site_name,
                keywords=query.keywords,
                error=str(exc),
            )
            errors.append(str(exc))
            new_listings = []

        # Log the scan
        duration = time.monotonic() - start_time
        scan_log = ScanLog(
            site=adapter.site_name,
            query_keywords=query.keywords,
            listings_found=len(new_listings),
            deals_found=0,  # Deals scored later at cycle level
            errors=errors,
            duration_seconds=duration,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )
        await self._scan_log_repo.save(scan_log)
        return new_listings

    async def _run_deal_radar(self, listings: list[Listing]) -> list:
        """Run SmartDealRadar on a batch of listings.

        Args:
            listings: Unmatched listings to evaluate.

        Returns:
            List of Deal objects found by the radar.
        """
        if not self._smart_deal_radar or not listings:
            return []

        deals = []
        to_evaluate = listings[: self._deal_radar_max_evaluations]

        log.info("Running SmartDealRadar", listings_count=len(to_evaluate))

        for listing in to_evaluate:
            try:
                deal = await self._smart_deal_radar.evaluate(listing)
                if deal is not None:
                    deals.append(deal)
            except Exception as exc:
                log.warning(
                    "SmartDealRadar evaluation failed",
                    listing_id=listing.id,
                    error=str(exc),
                )

        log.info(
            "SmartDealRadar complete",
            evaluated=len(to_evaluate),
            deals_found=len(deals),
        )
        return deals

    async def _dedup_and_save(self, listings: list[Listing]) -> list[Listing]:
        """Filter out already-seen listings and save new ones.

        Args:
            listings: Listings from a scan result.

        Returns:
            Only the newly saved listings.
        """
        new_listings: list[Listing] = []
        already_seen = 0
        for listing in listings:
            if listing.external_id and await self._listing_repo.exists(
                listing.site, listing.external_id
            ):
                already_seen += 1
                continue
            saved = await self._listing_repo.save(listing)
            new_listings.append(saved)

        log.info(
            "Dedup results",
            total_scraped=len(listings),
            new=len(new_listings),
            already_seen=already_seen,
        )
        return new_listings
