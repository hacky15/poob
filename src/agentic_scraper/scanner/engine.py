"""ScanEngine - orchestrates the full scan cycle."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentic_scraper.scanner.watchlist import WatchlistMatcher
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

    async def run_scan_cycle(self) -> None:
        """Execute one full scan cycle across all sites and queries."""
        log.info("Starting scan cycle")

        watch_items = await self._watchlist_repo.list_active()
        if not watch_items:
            log.info("No active watch items, skipping scan")
            return

        queries = self._matcher.build_queries(watch_items)

        for site_name in self._registry.list_sites():
            adapter = self._registry.get(site_name)
            if adapter is None:
                continue

            for query in queries:
                await self._run_single_scan(adapter, query, watch_items)

        log.info("Scan cycle complete")

    async def _run_single_scan(
        self, adapter: object, query: object, watch_items: list
    ) -> None:
        """Run a single scan for one (adapter, query) pair.

        Args:
            adapter: The site adapter to use.
            query: The scan query to execute.
            watch_items: Active watch items for matching.
        """
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc)
        errors: list[str] = []

        try:
            # Get the LLM chat model
            llm = getattr(self._llm, "chat_model", self._llm)

            result = await adapter.scan(query, self._browser, llm)

            # Deduplicate and save new listings
            new_listings = await self._dedup_and_save(result.listings)
            errors.extend(result.errors)

            # Match against watch items
            if new_listings:
                deals = self._matcher.match(new_listings, watch_items)
                matched_ids = {d.listing_id for d in deals}

                # Run SmartDealRadar on unmatched listings
                if self._smart_deal_radar:
                    unmatched = [
                        l for l in new_listings if l.id not in matched_ids
                    ]
                    radar_deals = await self._run_deal_radar(unmatched)
                    deals.extend(radar_deals)

                # Save and notify for each deal
                for deal in deals:
                    await self._deal_repo.save(deal)
                    # Find the listing for this deal
                    listing = next(
                        (l for l in new_listings if l.id == deal.listing_id),
                        None,
                    )
                    if listing:
                        await self._notifier.send_deal(deal, listing)

                deals_found = len(deals)
            else:
                deals_found = 0

        except Exception as exc:
            log.error(
                "Scan failed",
                site=adapter.site_name,
                keywords=query.keywords,
                error=str(exc),
            )
            errors.append(str(exc))
            new_listings = []
            deals_found = 0

        # Log the scan
        duration = time.monotonic() - start_time
        scan_log = ScanLog(
            site=adapter.site_name,
            query_keywords=query.keywords,
            listings_found=len(new_listings),
            deals_found=deals_found,
            errors=errors,
            duration_seconds=duration,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )
        await self._scan_log_repo.save(scan_log)

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
        for listing in listings:
            if listing.external_id and await self._listing_repo.exists(
                listing.site, listing.external_id
            ):
                continue
            saved = await self._listing_repo.save(listing)
            new_listings.append(saved)
        return new_listings
