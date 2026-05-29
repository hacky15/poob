"""PatrolEngine - orchestrates the patrol cycle.

GraphQL-first pipeline: intercept network responses for rich listing data,
fall back to DOM extraction when GraphQL yields nothing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from poob.browser.graphql_interceptor import (
    GraphQLListingData,
    parse_graphql_listings,
)
from poob.browser.stealth import random_delay
from poob.scanner.canary import CanaryRegistry
from poob.scanner.interest_matcher import InterestMatcher
from poob.scanner.listing_filter import (
    CategoryFilter,
    FilterChain,
    FilterStage,
    FreshnessFilter,
    GarbageFilter,
    GeoDistanceFilter,
    KnownFarLocationFilter,
    LocationTextFilter,
    SponsoredFilter,
)
from poob.scanner.observability import log_sweep, measure_sweep
from poob.sites.facebook.graphql_client import (
    AnonymousGraphQLClient,
    build_search_params,
    miles_to_km,
    resolve_center_coordinates,
)
from poob.sites.facebook.patrol_scanner import PatrolScanner, RadiusOscillator
from poob.storage.models import Deal, DealScore, Listing, ScanLog
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.config import AppConfig
    from poob.discord_bot.notifier import DealNotifier
    from poob.skills.orchestrator import SmartDealRadar
    from poob.storage.repositories.deal_repo import DealRepository
    from poob.storage.repositories.exclusion_repo import ExclusionRepository
    from poob.storage.repositories.listing_repo import ListingRepository
    from poob.storage.repositories.scan_log_repo import ScanLogRepository
    from poob.storage.repositories.watchlist_repo import WatchlistRepository

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
    # Funnel telemetry — populated by the engine at each pipeline stage
    # so the cycle can emit one structured "funnel.cycle" log line. The
    # gap between any two consecutive counts is the attrition at that
    # stage. Diagnosing "why aren't notifications firing" starts here.
    enriched: int = 0
    enrichment_redirected: int = 0
    enrichment_failed: int = 0
    timestamp_coverage_pct: int = 0  # % of enriched listings with posted_at
    vlm_evaluated: int = 0


def _graphql_to_listing(gql: GraphQLListingData) -> Listing:
    """Convert a GraphQLListingData into a Listing model."""
    raw_data: dict = {}
    if gql.condition:
        raw_data["condition"] = gql.condition
    if gql.posted_at:
        raw_data["posted_at_raw"] = gql.posted_at
    if gql.category_id:
        raw_data["category_id"] = gql.category_id
    if gql.delivery_types:
        raw_data["delivery_types"] = gql.delivery_types
    if gql.latitude is not None:
        raw_data["latitude"] = gql.latitude
    if gql.longitude is not None:
        raw_data["longitude"] = gql.longitude

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
        anonymous_browser: object | None = None,
        config: AppConfig,
    ) -> None:
        self._browser = browser_manager
        self._anonymous_browser = anonymous_browser
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
            scroll_steps=getattr(config, "patrol_scroll_steps_category", 20),
            search_scroll_steps=getattr(config, "patrol_scroll_steps_search", 30),
            days_since_listed=config.patrol_days_since_listed,
            fixed_radius=fixed_radius,
            scroll_until_stable=getattr(config, "patrol_scroll_until_stable", True),
            scroll_max_stable_checks=getattr(config, "patrol_scroll_max_stable_checks", 3),
            default_location_slug=getattr(config, "marketplace_default_location", None),
        )

        # Parse min score from config string
        self._min_score = DealScore(config.deal_radar_min_score)
        self._max_evaluations = config.deal_radar_max_evaluations
        # Enrichment cap (decoupled from the VLM eval cap). Enrichment is the
        # only source of posted_at for anon-GQL listings and yields a
        # timestamp ~100% of the time it runs, so we enrich a wider set than
        # we VLM-evaluate. Guard against a non-int (e.g. spec'd mock) or a
        # value below the eval cap.
        _ec = getattr(config, "patrol_enrichment_cap", None)
        self._enrichment_cap = (
            _ec if isinstance(_ec, int) and _ec >= self._max_evaluations
            else self._max_evaluations
        )

        # Build unified filter chain (replaces hardcoded _ALLOWED_NOTIFY_STATES,
        # _EXCLUDED_CATEGORY_PATTERNS, triple-check pattern, and backlog bypass).
        center_lat, center_lon = resolve_center_coordinates(
            city_slug=config.marketplace_default_location,
            explicit_lat=getattr(config, "patrol_center_lat", 0.0),
            explicit_lon=getattr(config, "patrol_center_lon", 0.0),
        )

        # General-browse search centers (e.g. Madison + Appleton). Each cycle
        # rotates to ONE center so per-cycle POST volume stays flat while
        # coverage spans all centers across cycles. The GeoDistanceFilter must
        # accept listings near ANY of them, else the non-primary metro's
        # listings get rejected ~100mi from the primary center.
        _browse_cfg = getattr(config, "patrol_browse_locations", None)
        if not isinstance(_browse_cfg, (list, tuple)) or not _browse_cfg:
            _browse_cfg = [config.marketplace_default_location]
        self._browse_locations: list[str] = [s for s in _browse_cfg if s]
        if not self._browse_locations:
            self._browse_locations = [config.marketplace_default_location]
        self._browse_location_idx = 0
        # Resolve each browse location to coords; the primary center is
        # already (center_lat, center_lon), so extras are the rest.
        extra_centers: list[tuple[float, float]] = []
        for slug in self._browse_locations:
            clat, clon = resolve_center_coordinates(city_slug=slug)
            if (round(clat, 4), round(clon, 4)) != (round(center_lat, 4), round(center_lon, 4)):
                extra_centers.append((clat, clon))

        # Learned location cache: locations rejected by GeoDistanceFilter
        # are cached so they skip enrichment on subsequent cycles.
        self._known_far_filter = KnownFarLocationFilter()

        self._filter_chain = FilterChain([
            # --- PRE_ENRICHMENT stage ---
            SponsoredFilter(),
            self._known_far_filter,  # Learned cache from previous geo_distance rejections
            LocationTextFilter(  # Coarse geo pre-check using state centroids
                center_lat=center_lat,
                center_lon=center_lon,
                radius_miles=float(config.patrol_base_radius_miles),
            ),
            CategoryFilter(),  # Uses category_id when available, keyword fallback
            FreshnessFilter(max_age_hours=config.listing_max_age_hours),
            # --- POST_ENRICHMENT stage ---
            GarbageFilter(),
            # Re-check category with enriched descriptions (keyword fallback only
            # fires when category_id is absent, so no double-rejection risk).
            CategoryFilter(
                name="category_post",
                stage=FilterStage.POST_ENRICHMENT,
            ),
            FreshnessFilter(
                name="freshness_post",
                stage=FilterStage.POST_ENRICHMENT,
                max_age_hours=config.listing_max_age_hours,
            ),
            GeoDistanceFilter(
                center_lat=center_lat,
                center_lon=center_lon,
                radius_miles=float(config.patrol_base_radius_miles),
                extra_centers=tuple(extra_centers),
            ),
        ])

        # Sweep mode: "unified" (1 page load) or "categories" (multi-page)
        self._sweep_mode = getattr(config, "patrol_sweep_mode", "unified")
        # Rotating DOM category index — each cycle hits a different category
        # page instead of the FB marketplace home page (which is dominated
        # by vehicles/housing/boats in WI and gives us almost no fresh
        # non-excluded listings). Rotation across the configured browse
        # categories pulls more diverse non-vehicle items via the DOM path
        # even when GraphQL is rate-limited.
        self._dom_category_idx = 0

        # Anonymous GraphQL client (depersonalized, no browser needed)
        self._graphql_enabled = getattr(config, "patrol_anonymous_graphql_enabled", True)
        self._graphql_client = AnonymousGraphQLClient(
            min_delay_seconds=getattr(config, "patrol_graphql_min_delay_seconds", 12.0),
        )

        # Shadow ban tracking
        self._consecutive_empty_sweeps = 0

        # Anonymous-browser self-heal. A degraded CDP session (Discord WS
        # reconnect, chromium crash, slow resource leak) leaves the DOM
        # sweep blocking until the 60s per-step timeout fires — and it never
        # recovers on its own. Observed in prod: the anon browser died
        # 2026-05-26 18:57 and logged 524 consecutive 60s timeouts over
        # ~3 days of ZERO ingestion until a manual container restart. After
        # this many consecutive timeouts, recreate the browser in-process.
        # See docs/incidents/anon-browser-cdp-death-no-recovery.
        self._anon_sweep_consecutive_timeouts = 0
        self._anon_sweep_max_timeouts = getattr(
            config, "patrol_anon_browser_max_timeouts", 3,
        )

        # Canary registry: ground-truth "did we see every listing" measurement.
        # Populated out-of-band by an admin command that registers a magic
        # token when the operator posts a dummy listing from a burner account.
        # Every raw sweep result is scanned for active tokens so a canary
        # detected by ANY path (anon GQL / anon DOM / watchlist) is recorded.
        self._canaries = CanaryRegistry()

    @property
    def canaries(self) -> CanaryRegistry:
        """Public read/write handle for admin canary registration commands."""
        return self._canaries

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
            # The authenticated browser is last-resort only (DOM fallback) —
            # the primary paths are anonymous GraphQL (no browser needed) and
            # the separate anonymous browser. If the main browser failed to
            # start (common on homelab: Chromium cold-start > 45s timeout),
            # we proceed with page=None and skip the auth-DOM fallback tier.
            page: object | None = None
            try:
                page = await self._browser.get_page()
            except Exception as exc:
                log.warning(
                    "Main browser unavailable — running anon-only",
                    error=str(exc)[:120],
                )

            # Pre-fetch known listing IDs for early-exit pagination.
            # On cycle 2+, most GQL results are already in the DB.
            # Passing known_ids lets pagination stop early when >80%
            # of a page is duplicates, cutting fetch time significantly.
            known_ids: set[str] = set()
            try:
                known_ids = await self._listing_repo.get_known_external_ids(
                    "facebook_marketplace", max_age_hours=48,
                )
                if known_ids:
                    log.info("Loaded known listing IDs for dedup", count=len(known_ids))
            except Exception:
                pass  # Non-critical — pagination still works without it

            # Step 1: Sweep + intercept (GraphQL primary, DOM fallback)
            all_listings, data_source = await self._sweep_and_intercept(
                page, result, known_ids=known_ids,
            )
            result.data_source = data_source

            # Step 1b: Watchlist keyword searches
            if getattr(self._config, "patrol_watchlist_sweep_enabled", True):
                watchlist_listings = await self._sweep_watchlist_items(
                    page, result, known_ids=known_ids,
                )
                if watchlist_listings:
                    log_sweep(
                        measure_sweep(
                            watchlist_listings,
                            days_since_listed=self._config.patrol_days_since_listed,
                        ),
                        source="watchlist_sweep",
                    )
                    self._canaries.check_batch(
                        watchlist_listings, source="watchlist_sweep",
                    )
                # Merge; dedup handles overlap below
                existing_ids = {l.external_id for l in all_listings if l.external_id}
                for wl in watchlist_listings:
                    if wl.external_id and wl.external_id not in existing_ids:
                        all_listings.append(wl)
                        existing_ids.add(wl.external_id)

            # Step 2: Batch dedup
            new_listings = await self._batch_dedup_and_save(all_listings, result)

            # Step 2b: Identify watchlist-matched listings for filter exemptions.
            # These listings get exempted from category and geo filters because:
            # 1. They were found via a user's targeted watchlist search (already
            #    geo-scoped to the user's location), so the global patrol center
            #    shouldn't reject them.
            # 2. Users may explicitly watch items in excluded categories.
            exempt_ids: set[str] = set()
            interests = await self._watchlist_repo.list_active()
            if interests:
                for listing in new_listings:
                    # Primary signal: tagged during watchlist sweep
                    if (listing.raw_data or {}).get("_watch_item_id"):
                        exempt_ids.add(listing.external_id)
                    # Secondary: title/description matches a watchlist interest
                    elif self._interest_matcher.match_single(listing, interests):
                        exempt_ids.add(listing.external_id)
                if exempt_ids:
                    log.info(
                        "Watchlist pre-filter exemptions",
                        exempt_count=len(exempt_ids),
                    )

            # Step 2c: Pre-enrichment filtering (unified filter chain).
            # Watchlist-matched listings are exempt from category and geo
            # filters — their search was already location-targeted by the
            # watchlist item's own config. This ensures a Kentucky user's
            # watchlist results aren't rejected by a Wisconsin-centered filter.
            _WATCHLIST_TAGS = frozenset({
                "watchlist_category_override",
                "watchlist_geo_override",
            })
            tags_by_id: dict[str, frozenset[str]] = {}
            for eid in exempt_ids:
                tags_by_id[eid] = _WATCHLIST_TAGS
            new_listings, pre_rejected = self._filter_chain.filter_batch(
                new_listings,
                stage=FilterStage.PRE_ENRICHMENT,
                tags_by_id=tags_by_id,
            )
            result.sponsored_filtered = sum(
                1 for _, r in pre_rejected
                if any(v.filter_name == "sponsored" for v in r.rejections)
            )

            # Step 2d: Sort order verification (diagnostic only)
            self._verify_sort_order(new_listings)

            # Step 2f: Pre-enrichment priority cap.
            # Apply the evaluation cap BEFORE detail enrichment to avoid wasting
            # browser time on listings that will be discarded. Previously 210+
            # listings were enriched via detail pages (15+ min of browser work)
            # only for the cap to discard all but 30 in _evaluate().
            # Watchlist-matched listings go first, then general listings.
            interests_for_sort = await self._watchlist_repo.list_active()
            if interests_for_sort:
                wl_first: list[Listing] = []
                wl_rest: list[Listing] = []
                for listing in new_listings:
                    watch_tag = (listing.raw_data or {}).get("_watch_item_id")
                    if watch_tag:
                        wl_first.append(listing)
                    else:
                        matches = self._interest_matcher.match_single(
                            listing, interests_for_sort
                        )
                        if matches:
                            wl_first.append(listing)
                        else:
                            wl_rest.append(listing)
                # Within each group, sort listings WITH timestamps before
                # those WITHOUT. Known-fresh listings get enriched first;
                # if the cap truncates, it's the timestamp-less (likely stale)
                # ones that get cut. Saves enrichment time.
                def _freshness_key(l: Listing) -> tuple[int, float]:
                    if l.posted_at is not None:
                        return (0, -l.posted_at.timestamp())
                    return (1, 0.0)

                wl_first.sort(key=_freshness_key)
                wl_rest.sort(key=_freshness_key)
                new_listings = wl_first + wl_rest
            else:
                # No watchlist items — still sort by freshness
                def _freshness_key(l: Listing) -> tuple[int, float]:
                    if l.posted_at is not None:
                        return (0, -l.posted_at.timestamp())
                    return (1, 0.0)

                new_listings.sort(key=_freshness_key)

            # Cap at the ENRICHMENT budget (>= eval cap), not the eval cap.
            # We enrich a wider freshness-sorted set to discover timestamps;
            # _evaluate() independently re-caps to the VLM eval budget. The
            # extra enriched listings are still saved (timestamped) for
            # backlog + future cycles even if they don't reach VLM this cycle.
            if len(new_listings) > self._enrichment_cap:
                log.info(
                    "Pre-enrichment cap applied",
                    before=len(new_listings),
                    after=self._enrichment_cap,
                    eval_cap=self._max_evaluations,
                )
                new_listings = new_listings[: self._enrichment_cap]

            # Step 2g: Enrich capped listings by visiting their detail pages.
            # The search grid gives title/price/location/image; detail pages
            # add the crucial field FB stripped from anonymous GQL in April
            # 2026: creation_time. Without it the notification gate blocks
            # 100% of deals as "no timestamp".
            #
            # Browser selection, in order of preference:
            #   1. main (authenticated) — richest data, no login modal.
            #   2. anonymous — data-sjs payload is still served to anon
            #      viewers; the login modal only overlays the visual UI.
            enrich_page = page
            enrich_source = "main"
            if enrich_page is None and self._anonymous_browser is not None:
                try:
                    enrich_page = await self._anonymous_browser.get_page()
                    enrich_source = "anonymous"
                    log.info(
                        "Detail-page enrichment: using anonymous browser "
                        "(main unavailable)",
                    )
                except Exception as exc:
                    log.warning(
                        "Anonymous browser page unavailable for enrichment",
                        error=str(exc)[:120],
                    )

            if enrich_page is not None:
                new_listings = await self._enrich_listings_from_detail_pages(
                    enrich_page, new_listings, result,
                )
                # Phase 1.75 ground truth: how many listings gained a
                # posted_at from enrichment? If this stays at 0, the
                # detail-page data-sjs payload is redacted for the
                # browser identity we're using — escalate from here.
                with_ts = sum(1 for lst in new_listings if lst.posted_at is not None)
                log.info(
                    "post_enrichment.timestamp_coverage",
                    source=enrich_source,
                    total=len(new_listings),
                    with_timestamp=with_ts,
                    no_timestamp=len(new_listings) - with_ts,
                )
                if new_listings:
                    result.timestamp_coverage_pct = round(
                        100.0 * with_ts / len(new_listings)
                    )
            else:
                log.info(
                    "Detail-page enrichment skipped — no browser available",
                    listings=len(new_listings),
                )

            # Step 2h: Post-enrichment filtering (unified filter chain).
            # Runs: GarbageFilter, CategoryFilter (with descriptions),
            # FreshnessFilter (with enriched timestamps), GeoDistanceFilter
            # (with coordinates from detail pages).
            new_listings, post_rejected = self._filter_chain.filter_batch(
                new_listings,
                stage=FilterStage.POST_ENRICHMENT,
                tags_by_id=tags_by_id,
            )

            # Learn: cache location strings rejected by geo_distance so
            # future cycles skip enrichment for the same locations.
            for listing, res in post_rejected:
                for v in res.rejections:
                    if v.filter_name == "geo_distance" and listing.location:
                        self._known_far_filter.learn(listing.location)

            # Step 3: Evaluate (SmartDealRadar + InterestMatcher)
            eval_result, all_evaluated = await self._evaluate(new_listings, result)

            # Step 4: Notify (public channel + user DMs)
            # Pass all_evaluated (includes backlog) so notification can find
            # any listing that produced a deal — not just current cycle's new listings.
            await self._notify(eval_result, all_evaluated, result)

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

        # Funnel telemetry — one line per cycle so each stage's attrition
        # is visible without grepping. Gap between consecutive counts is
        # where listings die; gap between vlm_evaluated and deals_found is
        # where the VLM cut things; gap between deals_found and
        # deals_notified is where the freshness/threshold gates cut things.
        log.info(
            "funnel.cycle",
            total_seen=result.total_listings_seen,
            new=result.new_listings,
            enriched=result.enriched,
            redirected=result.enrichment_redirected,
            enrich_failed=result.enrichment_failed,
            ts_coverage_pct=result.timestamp_coverage_pct,
            vlm_evaluated=result.vlm_evaluated,
            deals=result.deals_found,
            notified=result.deals_notified,
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

    async def _anon_dom_sweep(
        self, result: PatrolCycleResult,
    ) -> list[Listing]:
        """Anonymous browser DOM sweep, isolated for per-step timeout.

        Pulls a page from the anonymous BrowserManager, runs a category
        sweep, logs metrics + canary scan. Caller wraps in
        ``asyncio.wait_for`` so a hung CDP session doesn't eat the cycle.
        """
        anon_page = await self._anonymous_browser.get_page()
        listings = await self._sweep_categories(anon_page, result)
        if listings:
            log.info("Anonymous browser DOM sweep", count=len(listings))
            log_sweep(
                measure_sweep(
                    listings,
                    days_since_listed=self._config.patrol_days_since_listed,
                ),
                source="anonymous_dom",
            )
            self._canaries.check_batch(listings, source="anonymous_dom")
        return listings

    async def _auth_dom_sweep(
        self, result: PatrolCycleResult,
    ) -> list[Listing]:
        """Authenticated (logged-in) browser DOM sweep of the marketplace.

        The anonymous feed serves mostly stale listings; the LOGGED-IN
        marketplace "newest near you" feed is materially fresher. This sweeps
        it via the authenticated main browser. Isolated for a per-step
        timeout like the anon sweep. See
        docs/decisions/authenticated-discovery-sweep.md.
        """
        auth_page = await self._browser.get_page()
        listings = await self._sweep_categories(auth_page, result)
        if listings:
            log.info("Authenticated browser DOM sweep", count=len(listings))
            log_sweep(
                measure_sweep(
                    listings,
                    days_since_listed=self._config.patrol_days_since_listed,
                ),
                source="authenticated_dom",
            )
            self._canaries.check_batch(listings, source="authenticated_dom")
        return listings

    async def _restart_anonymous_browser(self) -> None:
        """Tear down and recreate the anonymous browser session in-process.

        Called after ``_anon_sweep_max_timeouts`` consecutive DOM-sweep
        timeouts. The degraded CDP session leaves ``get_page()`` and
        navigation blocking forever; recreating the BrowserSession restores
        ingestion without an operator-driven container restart.

        Both ``stop()`` and ``start()`` are bounded by their own timeouts —
        a hung session's ``stop()`` can itself block, and ``start()`` is
        replaced wholesale (``BrowserManager.start`` assigns a fresh
        ``BrowserSession``), so even a stop() that times out is recovered by
        the subsequent start(). See
        docs/incidents/anon-browser-cdp-death-no-recovery.
        """
        if self._anonymous_browser is None:
            return
        log.warning(
            "Recreating anonymous browser after consecutive sweep timeouts",
            consecutive_timeouts=self._anon_sweep_consecutive_timeouts,
        )
        try:
            await asyncio.wait_for(self._anonymous_browser.stop(), timeout=30.0)
        except Exception as exc:
            log.warning(
                "Anonymous browser stop during restart failed (continuing to start)",
                error=str(exc)[:100],
            )
        try:
            await asyncio.wait_for(self._anonymous_browser.start(), timeout=90.0)
            log.info("Anonymous browser recreated successfully")
            self._anon_sweep_consecutive_timeouts = 0
        except Exception as exc:
            # Leave the counter elevated so the next cycle retries the
            # restart rather than silently giving up.
            log.error(
                "Anonymous browser restart failed — will retry next cycle",
                error=str(exc)[:120],
            )

    async def _sweep_and_intercept(
        self, page: object, result: PatrolCycleResult,
        known_ids: set[str] | None = None,
    ) -> tuple[list[Listing], str]:
        """Fetch listings via anonymous GraphQL first, fall back to browser DOM.

        Priority order:
        1. Anonymous GraphQL direct POST (__user=0) — depersonalized, fast, rich data
        2. Browser JS GraphQL interception — captures what the browser fetches
        3. Browser DOM extraction — basic title/price/URL scraping

        Returns:
            Tuple of (listings, data_source).
        """
        # Step 1: Anonymous GraphQL category searches (__user=0)
        # Runs broad keyword searches (electronics, furniture, etc.) to get
        # diverse, depersonalized listings. This is the primary source.
        anon_listings = await self._fetch_anonymous_graphql(result, known_ids=known_ids)
        if anon_listings:
            log_sweep(
                measure_sweep(
                    anon_listings,
                    days_since_listed=self._config.patrol_days_since_listed,
                ),
                source="anonymous_graphql",
            )
            self._canaries.check_batch(anon_listings, source="anonymous_graphql")

        # Step 2: Anonymous browser DOM sweep (separate headless profile)
        # Uses a clean browser with NO login, NO cookies, NO search history
        # to get the true "newest listings" empty-query feed.
        anon_dom_listings: list[Listing] = []
        if self._anonymous_browser and getattr(
            self._config, "patrol_anonymous_browser_enabled", True
        ):
            # Per-step timeout — when the anonymous browser's CDP session
            # degrades after a Discord WebSocket reconnect, get_page() and
            # subsequent navigation calls block indefinitely. The 300s
            # scheduler timeout catches the runaway, but it eats the whole
            # cycle and the *next* cycle hangs the same way. Bounding the
            # browser branch at 60s lets the cycle fail fast and complete
            # the rest of the pipeline (eval, notify, scheduler progress).
            try:
                anon_dom_listings = await asyncio.wait_for(
                    self._anon_dom_sweep(result), timeout=60.0,
                )
                # Sweep completed (browser responsive, even if it found
                # nothing) — clear the hang counter.
                self._anon_sweep_consecutive_timeouts = 0
            except asyncio.TimeoutError:
                self._anon_sweep_consecutive_timeouts += 1
                log.warning(
                    "Anonymous browser DOM sweep timed out (60s) — "
                    "skipping this cycle's DOM tier",
                    consecutive_timeouts=self._anon_sweep_consecutive_timeouts,
                )
                # Self-heal: a hung CDP session never recovers on its own.
                # After N consecutive timeouts, recreate the browser so
                # ingestion resumes without a manual container restart.
                if (
                    self._anon_sweep_consecutive_timeouts
                    >= self._anon_sweep_max_timeouts
                ):
                    await self._restart_anonymous_browser()
            except Exception as exc:
                log.warning("Anonymous browser sweep failed", error=str(exc)[:100])

        # Step 2b: Authenticated DOM sweep of the logged-in marketplace
        # "newest near you" feed — materially fresher than the anonymous feed
        # (which serves mostly >6h-old listings; measured 0 fresh of 12 on
        # the anon path post-auth). Only runs when the main browser holds a
        # confirmed session. See docs/decisions/authenticated-discovery-sweep.md.
        auth_dom_listings: list[Listing] = []
        if self._browser and getattr(self._browser, "is_authenticated", False):
            try:
                auth_dom_listings = await asyncio.wait_for(
                    self._auth_dom_sweep(result), timeout=60.0,
                )
            except asyncio.TimeoutError:
                log.warning("Authenticated browser DOM sweep timed out (60s)")
            except Exception as exc:
                log.warning("Authenticated browser sweep failed", error=str(exc)[:100])

        # Merge: authenticated DOM (freshest) first, then anon GQL + anon DOM.
        combined: list[Listing] = []
        seen_ids: set[str] = set()
        for src in (auth_dom_listings, anon_listings, anon_dom_listings):
            for l in src:
                if l.external_id not in seen_ids:
                    seen_ids.add(l.external_id)
                    combined.append(l)

        if combined:
            log.info(
                "General browse complete",
                auth_count=len(auth_dom_listings),
                gql_count=len(anon_listings),
                dom_count=len(anon_dom_listings),
                total=len(combined),
            )
            self._consecutive_empty_sweeps = 0
            source = "authenticated" if auth_dom_listings else "anonymous_graphql"
            return combined, source

        # Step 3: Fall back to authenticated browser DOM (last resort).
        # Skip entirely if the main browser never came up — nothing below
        # this point works without a valid page object, and running with a
        # stale/unauthenticated session just wastes time.
        if page is None:
            log.info("Auth DOM fallback skipped — main browser unavailable")
            self._consecutive_empty_sweeps += 1
            return [], "no_data"

        try:
            await page.evaluate(self._INJECT_GQL_CAPTURE_JS)
        except Exception as exc:
            log.debug("GraphQL JS interceptor injection failed", error=str(exc))

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
            gql_ids = {l.external_id for l in graphql_listings}
            for dl in dom_listings:
                if dl.external_id and dl.external_id not in gql_ids:
                    graphql_listings.append(dl)
                    gql_ids.add(dl.external_id)
            data_source = "graphql" if len(gql_ids) > len(dom_listings) else "mixed"
            self._consecutive_empty_sweeps = 0
            return graphql_listings, data_source

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

    async def _fetch_anonymous_graphql(
        self, result: PatrolCycleResult,
        known_ids: set[str] | None = None,
    ) -> list[Listing]:
        """Fetch listings via anonymous GraphQL category searches.

        Runs depersonalized queries with __user=0 for multiple broad categories
        (electronics, furniture, appliances, etc.) to get diverse coverage of
        the newest marketplace listings without personalization bias.

        Returns:
            List of Listing objects, or empty list on failure.
        """
        if (
            not self._graphql_enabled
            or self._graphql_client.is_doc_id_broken
            or self._graphql_client.is_rate_limited
        ):
            if self._graphql_client.is_rate_limited:
                log.info(
                    "Anonymous GQL skipped — rate limited",
                    remaining_s=round(self._graphql_client.rate_limit_remaining_seconds),
                )
            return []

        categories = getattr(
            self._config, "patrol_anonymous_browse_categories",
            [""],  # Fallback: just the empty browse
        )

        # Rotate to ONE general-browse center this cycle (e.g. Madison this
        # cycle, Appleton next). Keeps POST volume flat at one-center x
        # N-categories while covering all configured metros across cycles —
        # the $0 way to scan two metros without doubling GQL requests (which
        # would trip FB's rate limit harder). Watchlist searches are
        # unaffected; they carry their own per-item location.
        browse_center = self._browse_locations[
            self._browse_location_idx % len(self._browse_locations)
        ]
        self._browse_location_idx += 1
        log.info(
            "Anonymous GQL browse center",
            center=browse_center,
            rotation_idx=(self._browse_location_idx - 1) % len(self._browse_locations),
            centers=len(self._browse_locations),
        )

        # Parallelize category fetches with semaphore (same pattern as
        # _sweep_watchlist_items). Cuts category phase from ~120s to ~40-60s.
        concurrency = getattr(self._config, "patrol_graphql_concurrency", 2)
        semaphore = asyncio.Semaphore(concurrency)
        all_results: list[list[GraphQLListingData]] = []

        async def _fetch_category(category: str) -> list[GraphQLListingData]:
            async with semaphore:
                # Short-circuit if a sibling task triggered the circuit breaker
                if self._graphql_client.is_rate_limited:
                    return []
                try:
                    # Thread THIS cycle's rotating browse center into the
                    # query. Without a location, build_search_params falls
                    # through to the hardcoded Appleton default (graphql_client
                    # DEFAULT_*), so FB served inventory outside the configured
                    # radius — the geo filter then discarded most of it after
                    # it had already flooded the eval pool. The watchlist path
                    # already threads location; the browse path silently
                    # didn't. See
                    # docs/incidents/browse-path-ignored-configured-location
                    # and docs/decisions/multi-center-general-browse.
                    params = build_search_params(
                        query=category,
                        location_slug=browse_center,
                        radius_miles=self._config.patrol_base_radius_miles,
                        days_listed=self._config.patrol_days_since_listed,
                        count=self._config.scan_max_listings_per_query,
                    )
                    return await self._graphql_client.search_all_pages(
                        params, max_pages=2, known_ids=known_ids,
                    )
                except Exception as exc:
                    log.warning(
                        "Anonymous GraphQL category failed",
                        category=category or "(browse)",
                        error=str(exc)[:100],
                    )
                    return []

        all_results = await asyncio.gather(
            *(_fetch_category(cat) for cat in categories)
        )

        # Merge and dedup across categories
        all_listings: list[Listing] = []
        seen_ids: set[str] = set()
        for cat_idx, gql_results in enumerate(all_results):
            new_count = 0
            for gql in gql_results:
                if gql.external_id not in seen_ids:
                    seen_ids.add(gql.external_id)
                    all_listings.append(_graphql_to_listing(gql))
                    new_count += 1
            if new_count:
                cat_name = categories[cat_idx] or "(browse)"
                log.info(
                    "Anonymous GraphQL category fetch",
                    category=cat_name,
                    new_listings=new_count,
                    total_so_far=len(all_listings),
                )

        if all_listings:
            result.categories_swept += len(categories)
            result.total_listings_seen += len(all_listings)
            log.info(
                "Anonymous GraphQL browse complete",
                categories_searched=len(categories),
                total_listings=len(all_listings),
            )

        return all_listings

    async def _sweep_categories(
        self, page: object, result: PatrolCycleResult
    ) -> list[Listing]:
        """Sweep categories via DOM extraction (also triggers GraphQL requests)."""
        all_listings: list[Listing] = []
        seen_external_ids: set[str] = set()

        if self._sweep_mode == "unified":
            # Unified mode used to sweep ONLY the FB marketplace home page
            # (`category=None`), which in WI is dominated by vehicles,
            # housing, and boats — all excluded categories. The non-excluded
            # listings that DO appear were vehicle-feed bycatch and almost
            # never fresh non-excluded items.
            #
            # Now: rotate through the configured browse categories one per
            # cycle. Each cycle hits a different category-anchored page
            # (e.g. /marketplace/madison/category/electronics) which is far
            # less vehicle-dominated. The home page (`None`) is still in
            # the rotation but is only 1 of N slots instead of every cycle.
            rotation = list(self._config.patrol_anonymous_browse_categories or [""])
            if not rotation:
                rotation = [""]
            slot = rotation[self._dom_category_idx % len(rotation)]
            self._dom_category_idx += 1
            # Empty string in the config means "home page" (no category anchor).
            categories: list[str | None] = [slot if slot else None]
            log.info(
                "DOM sweep rotating category",
                category=slot or "(home page)",
                rotation_idx=(self._dom_category_idx - 1) % len(rotation),
                rotation_size=len(rotation),
            )
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
        self, page: object, result: PatrolCycleResult,
        known_ids: set[str] | None = None,
    ) -> list[Listing]:
        """Search Facebook for each active watchlist item.

        Two-phase approach for speed:
        1. Run all GraphQL searches concurrently (semaphore-limited, ~2-3 at once)
        2. Run DOM fallback serially only for searches where GQL got < 5 results

        This cuts watchlist sweep from ~8 min to ~2-3 min without sacrificing
        coverage — GraphQL pagination gets 50-100 listings per search, and DOM
        only kicks in when GQL fails.

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

        gql_watchlist_enabled = (
            self._graphql_enabled
            and getattr(self._config, "patrol_graphql_watchlist_enabled", True)
            and not self._graphql_client.is_doc_id_broken
            and not self._graphql_client.is_rate_limited
        )

        # Build the full list of (item, cfg) pairs to search
        search_tasks: list[tuple[object, dict]] = []
        for item in interests:
            configs = item.search_configs if item.search_configs else [{}]
            for cfg in configs:
                search_tasks.append((item, cfg))

        # --- Phase 1: Concurrent GraphQL searches ---
        gql_concurrency = getattr(self._config, "patrol_graphql_concurrency", 2)
        semaphore = asyncio.Semaphore(gql_concurrency)
        max_pages = getattr(self._config, "patrol_graphql_max_pages", 3)

        # Results indexed by task position
        gql_results_map: dict[int, list[Listing]] = {}

        async def _gql_search(idx: int, item: object, cfg: dict) -> None:
            """Run a single GraphQL search under the semaphore."""
            if not gql_watchlist_enabled or self._graphql_client.is_rate_limited:
                gql_results_map[idx] = []
                return
            async with semaphore:
                try:
                    location = (
                        cfg.get("location")
                        or item.location
                        or getattr(self._config, "marketplace_default_location", None)
                    )
                    radius = cfg.get("radius_miles") or self._config.patrol_base_radius_miles
                    gql_params = build_search_params(
                        query=item.interest,
                        location_slug=location,
                        radius_miles=radius,
                        max_price=cfg.get("max_price", item.max_price),
                        min_price=cfg.get("min_price"),
                        days_listed=self._config.patrol_days_since_listed,
                        condition=cfg.get("condition"),
                        count=self._config.scan_max_listings_per_query,
                    )
                    raw = await self._graphql_client.search_all_pages(
                        gql_params, max_pages=max_pages,
                        known_ids=known_ids,
                    )
                    listings = [_graphql_to_listing(g) for g in raw]
                    gql_results_map[idx] = listings
                    if listings:
                        log.info(
                            "Watchlist GraphQL success",
                            interest=item.interest,
                            count=len(listings),
                        )
                except Exception as exc:
                    log.debug(
                        "Watchlist GraphQL failed",
                        interest=item.interest,
                        error=str(exc)[:100],
                    )
                    gql_results_map[idx] = []

        # Launch all GraphQL searches concurrently
        gql_coros = [
            _gql_search(i, item, cfg)
            for i, (item, cfg) in enumerate(search_tasks)
        ]
        await asyncio.gather(*gql_coros)

        # --- Phase 2: Serial DOM fallback for searches that need it ---
        all_search_listings: list[Listing] = []
        seen_ids: set[str] = set()
        searches_run = 0
        dom_searches = 0

        for idx, (item, cfg) in enumerate(search_tasks):
            try:
                gql_listings = gql_results_map.get(idx, [])

                location = (
                    cfg.get("location")
                    or item.location
                    or getattr(self._config, "marketplace_default_location", None)
                )

                # DOM fallback only when GQL got < 5 results AND the auth
                # browser page is actually available. If the main browser
                # failed to start, GQL-only results stand alone.
                dom_listings: list[Listing] = []
                if len(gql_listings) < 5 and page is not None:
                    dom_listings = await self._scanner.sweep_search(
                        page,
                        item.interest,
                        max_price=cfg.get("max_price", item.max_price),
                        min_price=cfg.get("min_price"),
                        location_slug=location,
                        condition=cfg.get("condition"),
                        radius_miles=cfg.get("radius_miles"),
                    )
                    dom_searches += 1
                    # Stealth delay only for browser-based searches
                    await random_delay(
                        self._config.patrol_inter_category_delay_min_ms,
                        self._config.patrol_inter_category_delay_max_ms,
                    )

                # Merge: GraphQL primary, DOM fills gaps
                merged: list[Listing] = list(gql_listings)
                gql_ids = {l.external_id for l in gql_listings if l.external_id}
                for dl in dom_listings:
                    if dl.external_id and dl.external_id not in gql_ids:
                        merged.append(dl)
                        gql_ids.add(dl.external_id)

                for listing in merged:
                    if listing.external_id and listing.external_id not in seen_ids:
                        seen_ids.add(listing.external_id)
                        listing.raw_data["_watch_item_id"] = item.id
                        listing.raw_data["_watch_interest"] = item.interest
                        all_search_listings.append(listing)
                searches_run += 1

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
            gql_searches=len(search_tasks),
            dom_fallback_searches=dom_searches,
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

    # Maximum concurrent tabs for detail page enrichment.
    # 2 is safe for a single logged-in account — mimics a human opening
    # a couple of tabs to compare listings. 3+ risks detection.
    _ENRICHMENT_CONCURRENCY = 2

    async def _enrich_listings_from_detail_pages(
        self,
        page: object,
        listings: list[Listing],
        result: PatrolCycleResult | None = None,
    ) -> list[Listing]:
        """Visit detail pages to enrich new listings with descriptions.

        Uses 2-tab parallelism to cut enrichment time by ~30-40%.
        Tabs are staggered and inter-batch delays randomized to mimic
        natural browsing (right-click → open in new tab pattern).

        The search results grid only provides title, price, location, and a
        thumbnail.  Descriptions, condition, seller info, and additional images
        are only available on individual listing detail pages via Open Graph
        meta tags and JSON-LD structured data.

        Listings that already have a description (e.g. from GraphQL intercept)
        are skipped to avoid unnecessary page navigations.

        Args:
            page: Browser page for navigation.
            listings: All new listings from this patrol cycle.

        Returns:
            The same list with listings replaced by enriched versions.
        """
        from dataclasses import replace as _replace

        from poob.sites.facebook.detail_extractor import (
            EnrichmentRedirectedError,
            extract_listing_details,
        )

        if not listings:
            return listings

        # Identify which listings need enrichment.
        # Skip listings that already have a meaningful description (GraphQL source).
        needs_enrichment: list[int] = []
        for i, listing in enumerate(listings):
            if not listing.listing_url:
                continue
            # Already has a real description — skip
            if listing.description and len(listing.description.strip()) > 10:
                continue
            needs_enrichment.append(i)

        if not needs_enrichment:
            log.info(
                "All listings already enriched, skipping detail page visits",
                total=len(listings),
            )
            return listings

        concurrency = self._ENRICHMENT_CONCURRENCY
        log.info(
            "Enriching listings via detail pages",
            to_enrich=len(needs_enrichment),
            already_enriched=len(listings) - len(needs_enrichment),
            total=len(listings),
            concurrency=concurrency,
        )

        enriched_count = 0
        failed_count = 0
        redirected_count = 0

        # Per-listing timeout: bounds each detail-page navigation so one
        # stuck page can't stall the batch. A page that hits this timeout
        # is counted as a failure (consecutive_misses increments) so the
        # early-bail kicks in if many pages hang in a row.
        _PER_LISTING_TIMEOUT_S = 20.0

        async def _enrich_one(idx: int, tab: object) -> None:
            """Enrich a single listing using the given browser tab."""
            nonlocal enriched_count, failed_count, redirected_count
            listing = listings[idx]
            try:
                enriched = await asyncio.wait_for(
                    extract_listing_details(tab, listing),
                    timeout=_PER_LISTING_TIMEOUT_S,
                )
                got_new_data = (
                    (enriched.description and not listing.description)
                    or enriched.title != listing.title
                    or enriched.price != listing.price
                    or (enriched.posted_at is not None and listing.posted_at is None)
                )
                if got_new_data:
                    listings[idx] = enriched
                    enriched_count += 1
                    try:
                        await self._listing_repo.save(enriched)
                    except Exception:
                        pass
                    log.debug(
                        "Listing enriched from detail page",
                        external_id=listing.external_id,
                        title=enriched.title[:50],
                        has_description=bool(enriched.description),
                        desc_len=len(enriched.description or ""),
                        price=enriched.price,
                    )
            except EnrichmentRedirectedError as exc:
                # FB served a "similar item" recommendation page — the
                # original listing is sold/deleted/private. Mark evaluated
                # so it never reappears as backlog, flag for VLM exclusion
                # this cycle, and count as a redirect (not a failure).
                redirected_count += 1
                listings[idx] = _replace(
                    listing,
                    raw_data={
                        **(listing.raw_data or {}),
                        "_enrichment_redirected": True,
                    },
                )
                if listing.id:
                    try:
                        await self._listing_repo.mark_evaluated([listing.id])
                    except Exception:
                        pass
                log.info(
                    "Detail enrichment redirected — skipping VLM",
                    external_id=listing.external_id,
                    reason=str(exc)[:120],
                )
            except asyncio.TimeoutError:
                failed_count += 1
                log.warning(
                    "Detail enrichment timed out",
                    external_id=listing.external_id,
                    timeout_s=_PER_LISTING_TIMEOUT_S,
                )
            except Exception as exc:
                failed_count += 1
                log.warning(
                    "Detail enrichment failed",
                    external_id=listing.external_id,
                    error=str(exc)[:100],
                )

        session = self._browser.get_session()

        # Create extra tabs for parallel enrichment.
        # All tabs share the same BrowserContext (cookies/session).
        extra_tabs: list[object] = []
        try:
            for _ in range(concurrency - 1):
                tab = await session.new_page()
                extra_tabs.append(tab)
        except Exception as exc:
            log.warning(
                "Could not create extra tabs, falling back to sequential",
                error=str(exc)[:100],
            )

        all_tabs = [page, *extra_tabs]
        batch_count = 0
        # Early bail: if the first N listings all fail to enrich, the
        # extraction method probably doesn't work on this session's pages.
        # Stop wasting time navigating to 50 pages that return nothing.
        _EARLY_BAIL_THRESHOLD = 5  # Give up after 5 consecutive misses
        consecutive_misses = 0

        # Process in batches of `concurrency` with staggered starts.
        for batch_start in range(0, len(needs_enrichment), len(all_tabs)):
            # Early bail check
            if consecutive_misses >= _EARLY_BAIL_THRESHOLD:
                skipped = len(needs_enrichment) - batch_start
                log.warning(
                    "Detail enrichment early bail — extraction not working",
                    consecutive_misses=consecutive_misses,
                    attempted=batch_start,
                    skipped=skipped,
                )
                break

            prev_enriched = enriched_count
            batch = needs_enrichment[batch_start : batch_start + len(all_tabs)]
            batch_count += 1

            if len(batch) == 1:
                # Single item — no parallelism needed
                await _enrich_one(batch[0], all_tabs[0])
            else:
                # Stagger tab starts by 1-2s to look natural
                tasks = []
                for j, idx in enumerate(batch):
                    if j > 0:
                        await random_delay(1000, 2000)
                    tasks.append(asyncio.create_task(_enrich_one(idx, all_tabs[j])))
                await asyncio.gather(*tasks)

            # Track consecutive misses for early bail
            if enriched_count > prev_enriched:
                consecutive_misses = 0  # Reset on any success
            else:
                consecutive_misses += len(batch)

            # Inter-batch delay: 2-4s randomized, with occasional longer pause
            if batch_start + len(all_tabs) < len(needs_enrichment):
                if batch_count % 7 == 0:
                    # Every ~7th batch, take a longer break to break pattern
                    await random_delay(4000, 7000)
                else:
                    await random_delay(2000, 4000)

        # Clean up extra tabs
        for tab in extra_tabs:
            try:
                await session.close_page(tab)
            except Exception:
                pass
        log.info(
            "Detail enrichment complete",
            attempted=len(needs_enrichment),
            enriched=enriched_count,
            failed=failed_count,
            redirected=redirected_count,
            concurrency=len(all_tabs),
            batches=batch_count,
        )
        if result is not None:
            result.enriched = enriched_count
            result.enrichment_redirected = redirected_count
            result.enrichment_failed = failed_count
        return listings

    # Garbage/stale/category/location filtering is now handled by the unified
    # FilterChain (see listing_filter.py). The old _filter_garbage_listings,
    # _filter_stale, and _filter_excluded_categories methods have been removed.

    async def _evaluate(
        self, listings: list[Listing], result: PatrolCycleResult
    ) -> tuple[EvaluationResult, list[Listing]]:
        """Run deal evaluation pipeline on new listings + backlog.

        SmartDealRadar handles text triage, visual enrichment, comparable
        sales, and VLM evaluation. Watchlist matches are prioritized.
        Unevaluated listings from previous cycles are included in the backlog.
        """
        interests = await self._watchlist_repo.list_active()

        # Drop listings whose detail-page enrichment redirected to a
        # different listing (FB serves a recommendation feed when the
        # original is sold/deleted/private). They have no description and
        # no verified timestamp, so VLM evaluation is wasted and the
        # freshness gate auto-rejects them. They were already marked
        # evaluated in _enrich_one so they won't reappear as backlog.
        pre_redirect_count = len(listings)
        listings = [
            l for l in listings
            if not (l.raw_data or {}).get("_enrichment_redirected")
        ]
        redirected_dropped = pre_redirect_count - len(listings)
        if redirected_dropped:
            log.info(
                "Excluded redirected listings from VLM",
                dropped=redirected_dropped,
                remaining=len(listings),
            )

        # Prioritize: watchlist-matched listings first, then the rest.
        # A listing is watchlist-matched if either:
        #   1. It was found via watchlist keyword search (_watch_item_id tag), OR
        #   2. Its title/description matches a watchlist interest (keyword matching)
        if interests:
            watchlist_matched: list[Listing] = []
            non_matched: list[Listing] = []
            for listing in listings:
                # Primary signal: tagged during watchlist sweep
                watch_tag = (listing.raw_data or {}).get("_watch_item_id")
                if watch_tag:
                    watchlist_matched.append(listing)
                else:
                    # Fallback: keyword matching for general sweep results
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

        # Fetch unevaluated backlog from previous cycles (cap overflow recovery).
        # These are listings that were saved to DB but exceeded the eval cap.
        # CRITICAL: Backlog listings go through the SAME filter chain as fresh
        # listings — they are no longer exempt from category, location, or
        # freshness checks. This was the root cause of vehicles, stale listings,
        # and out-of-region listings reaching notifications (Issue #8).
        backlog: list[Listing] = []
        try:
            raw_backlog = await self._listing_repo.get_unevaluated(
                site="facebook_marketplace",
                max_age_hours=24,
                limit=20,
            )
            current_ids = {l.id for l in prioritized}
            raw_backlog = [l for l in raw_backlog if l.id not in current_ids]

            if raw_backlog:
                log.info("Backlog: filtering through full pipeline", count=len(raw_backlog))

                # Run both filter stages on backlog (they have enriched data from DB).
                backlog, pre_rejected = self._filter_chain.filter_batch(
                    raw_backlog, stage=FilterStage.PRE_ENRICHMENT,
                )
                backlog, post_rejected = self._filter_chain.filter_batch(
                    backlog, stage=FilterStage.POST_ENRICHMENT,
                )

                # Mark rejected backlog as evaluated so they don't reappear.
                rejected_ids = [
                    l.id for l, _ in pre_rejected + post_rejected if l.id
                ]
                if rejected_ids:
                    await self._listing_repo.mark_evaluated(rejected_ids)
                    log.info(
                        "Backlog filtered and marked evaluated",
                        rejected=len(rejected_ids),
                        kept=len(backlog),
                    )
                elif backlog:
                    log.info(
                        "Backlog listings recovered for evaluation",
                        backlog_count=len(backlog),
                    )
        except Exception as exc:
            log.warning("Failed to fetch backlog", error=str(exc)[:100])

        # Gate: backlog listings without a verified timestamp were never
        # enriched — they have no freshness data, no description, and are
        # the primary source of stale junk notifications. Only allow
        # timestamp-verified backlog listings into evaluation.
        if backlog:
            pre_gate = len(backlog)
            backlog = [l for l in backlog if l.posted_at is not None]
            gated = pre_gate - len(backlog)
            if gated:
                log.info(
                    "Backlog timestamp gate",
                    blocked=gated,
                    passed=len(backlog),
                )

        # Combine: new prioritized listings first, then backlog.
        # Reserve slots for backlog so cap overflow from previous cycles
        # actually gets a second chance.  Without this, when new >= max_eval
        # the backlog is entirely excluded and never evaluated.
        backlog_slots = min(len(backlog), max(5, self._max_evaluations // 5))
        new_slots = self._max_evaluations - backlog_slots
        capped_new = prioritized[:new_slots]
        capped_backlog = backlog[:backlog_slots]
        to_evaluate = capped_new + capped_backlog

        eval_result = EvaluationResult()

        if not self._smart_deal_radar or not to_evaluate:
            return eval_result, to_evaluate

        result.vlm_evaluated = len(to_evaluate)

        try:
            pipeline_results = await self._smart_deal_radar.evaluate_batch(
                to_evaluate, watchlist_items=interests
            )
        except Exception as exc:
            log.error("Pipeline evaluation failed", error=str(exc))
            result.errors.append(f"Pipeline: {exc}")
            return eval_result, to_evaluate

        # Mark all evaluated listings so they aren't retried
        evaluated_ids = [l.id for l in to_evaluate if l.id]
        try:
            await self._listing_repo.mark_evaluated(evaluated_ids)
        except Exception as exc:
            log.warning("Failed to mark listings evaluated", error=str(exc)[:100])

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
            backlog_included=len(capped_backlog),
            base_deals=len(eval_result.base_deals),
            watchlist_deals=len(eval_result.watchlist_deals),
        )
        return eval_result, to_evaluate

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

        Public channel: deals at ``deal_public_min_score`` (default INCREDIBLE)
        AND posted within ``public_notification_max_age_minutes`` (default 10).
        Watchlist DMs: deals at per-user ``notification_threshold`` (default GOOD)
        AND posted within ``watchlist_notification_max_age_minutes`` (default 30).

        The evaluation cutoff (``listing_max_age_hours``) lets older listings
        through for backlog recovery and measurement; these minute-level
        cutoffs are what actually gate user-visible notifications.
        """
        listing_map = {l.id: l for l in listings if l.id}
        exclusions = await self._load_exclusion_keywords()

        public_min = DealScore(
            getattr(self._config, "deal_public_min_score", "incredible")
        )
        watchlist_min = DealScore(
            getattr(self._config, "deal_watchlist_min_score", "good")
        )
        public_max_age_min = float(getattr(
            self._config, "public_notification_max_age_minutes", 10,
        ))
        watchlist_max_age_min = float(getattr(
            self._config, "watchlist_notification_max_age_minutes", 30,
        ))
        now = datetime.now(timezone.utc)

        def _age_minutes(l: Listing) -> float | None:
            if l.posted_at is None:
                return None
            return (now - l.posted_at).total_seconds() / 60.0

        # Public channel: non-watchlist deals at INCREDIBLE threshold
        global_excluded = exclusions.get("__global__", set())
        for deal in eval_result.base_deals:
            listing = listing_map.get(deal.listing_id)
            # Freshness gate: never notify for listings with unverified age.
            if listing and listing.posted_at is None:
                log.info(
                    "notify.blocked_no_timestamp",
                    title=(listing.title or "")[:50],
                    score=deal.score.value,
                )
                continue
            # Minute-level freshness gate — "just listed" product rule.
            age_min = _age_minutes(listing) if listing else None
            if age_min is None or age_min > public_max_age_min:
                log.info(
                    "notify.skip_too_old",
                    channel="public",
                    title=(listing.title[:50] if listing else "?"),
                    age_minutes=round(age_min, 1) if age_min is not None else None,
                    limit_minutes=public_max_age_min,
                    score=deal.score.value,
                )
                continue
            if _SCORE_RANK.get(deal.score, 0) < _SCORE_RANK.get(public_min, 0):
                log.info(
                    "notify.skip_base_deal",
                    title=(listing.title[:50] if listing else "?"),
                    price=listing.price if listing else None,
                    score=deal.score.value,
                    required=public_min.value,
                    reason="below_public_threshold",
                )
                continue
            listing = listing_map.get(deal.listing_id)
            if listing and self._is_excluded(listing, global_excluded):
                log.debug("Deal excluded by keyword", title=listing.title[:40])
                continue
            # No safety nets needed — backlog listings now go through the
            # full filter chain (category + geo + freshness + garbage) before
            # evaluation, so excluded categories and out-of-region listings
            # are caught upstream.
            try:
                await self._deal_repo.save(deal)
                if listing:
                    await self._notifier.send_deal(deal, listing)
                    await self._deal_repo.mark_notified(deal.id)
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
            # Freshness gate: never notify for listings with unverified age.
            if listing.posted_at is None:
                log.info(
                    "notify.blocked_no_timestamp",
                    title=(listing.title or "")[:50],
                    score=deal.score.value,
                    interest=deal.watch_item_id,
                )
                continue
            # Minute-level freshness gate — "just listed" for watchlist DMs.
            age_min = _age_minutes(listing)
            if age_min is None or age_min > watchlist_max_age_min:
                log.info(
                    "notify.skip_too_old",
                    channel="watchlist_dm",
                    title=(listing.title or "")[:50],
                    age_minutes=round(age_min, 1) if age_min is not None else None,
                    limit_minutes=watchlist_max_age_min,
                    score=deal.score.value,
                    interest=deal.watch_item_id,
                )
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
                        log.info(
                            "notify.skip_watchlist",
                            title=listing.title[:50],
                            price=listing.price,
                            score=deal.score.value,
                            interest=watch_item.interest,
                            reason="threshold_is_free_but_price>0",
                        )
                        continue
                elif user_threshold == "all":
                    pass  # Notify for everything that matched
                else:
                    # Check deal quality against user's threshold
                    deal_rank = threshold_rank.get(deal.score.value, 0)
                    min_rank = threshold_rank.get(user_threshold, 2)
                    if deal_rank < min_rank:
                        log.info(
                            "notify.skip_watchlist",
                            title=listing.title[:50],
                            price=listing.price,
                            score=deal.score.value,
                            interest=watch_item.interest,
                            reason="below_user_threshold",
                            user_threshold=user_threshold,
                        )
                        continue

                await self._deal_repo.save(deal)
                await self._notifier.send_deal_dm(
                    deal,
                    listing,
                    discord_user_id=int(watch_item.discord_user_id),
                    watch_interest=watch_item.interest,
                )
                await self._deal_repo.mark_notified(deal.id)
                result.deals_notified += 1
            except Exception as exc:
                log.error(
                    "Watchlist DM notification failed",
                    deal_listing_id=deal.listing_id,
                    watch_item_id=deal.watch_item_id,
                    error=str(exc),
                )
