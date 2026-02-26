"""Facebook Marketplace site adapter."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentic_scraper.sites.base import ScanQuery, ScanResult
from agentic_scraper.sites.facebook.parser import parse_listings
from agentic_scraper.sites.facebook.prompts import DETAIL_PROMPT, build_search_prompt
from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("sites.facebook.adapter")


class FacebookMarketplaceAdapter:
    """Site adapter for Facebook Marketplace.

    Handles navigation, search, and listing extraction from Facebook
    Marketplace using a browser-use agent driven by an LLM.
    """

    @property
    def site_name(self) -> str:
        """Unique identifier for this site."""
        return "facebook_marketplace"

    @property
    def base_url(self) -> str:
        """Base URL for Facebook Marketplace."""
        return "https://www.facebook.com/marketplace"

    @property
    def requires_login(self) -> bool:
        """Facebook requires login to browse Marketplace."""
        return True

    async def login(self, browser_manager: object) -> bool:
        """Authenticate with Facebook.

        Uses persistent cookies from browser profile. If cookies are expired,
        the user needs to manually log in once with the browser visible.

        Args:
            browser_manager: BrowserManager instance.

        Returns:
            True (assumes cookies are valid; manual login is a fallback).
        """
        # With persistent cookie profiles, login state is maintained across
        # sessions. If cookies expire, user must log in manually once with
        # headless=False. Future enhancement: detect login state and prompt.
        log.info("Facebook login - using persistent cookie profile")
        return True

    async def scan(
        self, query: ScanQuery, browser_manager: object, llm: BaseChatModel
    ) -> ScanResult:
        """Search Facebook Marketplace and extract listings.

        Args:
            query: Search parameters.
            browser_manager: BrowserManager for creating agents.
            llm: LangChain chat model for the browser-use agent.

        Returns:
            ScanResult with parsed listings and any errors.
        """
        start_time = time.monotonic()
        errors: list[str] = []

        prompt = build_search_prompt(
            keywords=query.keywords,
            max_price=query.max_price,
            location=query.location,
        )

        try:
            agent = browser_manager.create_agent(task=prompt, llm=llm)
            history = await agent.run()

            raw_result = history.final_result()
            if not history.is_successful() or not raw_result:
                errors.append("Agent did not complete successfully")
                log.warning("Facebook scan agent failed", keywords=query.keywords)
                return ScanResult(
                    listings=[],
                    errors=errors,
                    scan_duration_seconds=time.monotonic() - start_time,
                )

            listings = parse_listings(raw_result, site=self.site_name)

        except Exception as exc:
            errors.append(f"Scan failed: {exc}")
            log.error("Facebook scan error", error=str(exc), keywords=query.keywords)
            listings = []

        duration = time.monotonic() - start_time
        log.info(
            "Facebook scan complete",
            keywords=query.keywords,
            listings_found=len(listings),
            duration=f"{duration:.1f}s",
        )

        return ScanResult(
            listings=listings,
            errors=errors,
            scan_duration_seconds=duration,
        )

    async def get_listing_details(
        self, listing_url: str, browser_manager: object, llm: BaseChatModel
    ) -> Listing:
        """Navigate to a single listing and extract full details.

        Args:
            listing_url: Direct URL to the Facebook Marketplace listing.
            browser_manager: BrowserManager for creating agents.
            llm: LangChain chat model.

        Returns:
            Listing with all available details.
        """
        prompt = DETAIL_PROMPT.format(listing_url=listing_url)
        agent = browser_manager.create_agent(task=prompt, llm=llm)
        history = await agent.run()

        raw_result = history.final_result()
        if raw_result:
            listings = parse_listings(raw_result, site=self.site_name)
            if listings:
                return listings[0]

        # Return a minimal listing if extraction failed
        return Listing(site=self.site_name, listing_url=listing_url)
