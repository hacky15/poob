"""Facebook Marketplace site adapter."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentic_scraper.sites.base import ScanQuery, ScanResult
from agentic_scraper.sites.facebook.direct_scanner import DirectScanner
from agentic_scraper.sites.facebook.parser import parse_listings
from agentic_scraper.sites.facebook.prompts import DETAIL_PROMPT, build_search_prompt
from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from typing import Any

    BaseChatModel = Any

log = get_logger("sites.facebook.adapter")


class FacebookMarketplaceAdapter:
    """Site adapter for Facebook Marketplace.

    Supports two scan modes:
    - "direct" (default): Uses CDP Page for navigation and JS for extraction.
      Fast (~10s), zero LLM calls for navigation. Falls back to agent on error.
    - "agent": Uses browser-use LLM Agent for navigation (legacy path).
      Slower (~3min), requires NVIDIA NIM or compatible structured-output LLM.
    """

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._max_listings: int = 20
        self._scan_mode: str = "direct"
        self._json_llm: Any | None = None

    def set_credentials(self, email: str, password: str) -> None:
        """Set Facebook login credentials for auto-login during scans."""
        self._email = email or None
        self._password = password or None

    def set_max_listings(self, max_listings: int) -> None:
        """Set the maximum number of listings to extract per scan."""
        self._max_listings = max_listings

    def set_scan_mode(self, mode: str) -> None:
        """Set the scan mode.

        Args:
            mode: "direct" for CDP-based scanning (fast, default),
                  "agent" for browser-use LLM agent (legacy).
        """
        if mode not in ("direct", "agent"):
            raise ValueError(f"Invalid scan mode: {mode!r}. Use 'direct' or 'agent'.")
        self._scan_mode = mode
        log.info("Scan mode set", mode=mode)

    def set_json_llm(self, llm: Any) -> None:
        """Set the JSON LLM for extraction fallback in direct mode.

        Args:
            llm: LangChain-compatible chat model with JSON output format.
        """
        self._json_llm = llm

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
        log.info("Facebook login - using persistent cookie profile")
        return True

    async def scan(
        self, query: ScanQuery, browser_manager: object, llm: BaseChatModel
    ) -> ScanResult:
        """Search Facebook Marketplace and extract listings.

        Uses direct CDP navigation by default (fast, no LLM waste).
        Falls back to browser-use agent if direct scan fails.

        Args:
            query: Search parameters.
            browser_manager: BrowserManager for page access or agent creation.
            llm: Chat model for browser-use agent (used in agent mode or fallback).

        Returns:
            ScanResult with parsed listings and any errors.
        """
        if self._scan_mode == "direct":
            result = await self._scan_direct(query, browser_manager)
            if result.listings or not result.errors:
                return result
            log.warning(
                "Direct scan failed, falling back to agent",
                errors=result.errors,
                keywords=query.keywords,
            )

        return await self._scan_agent(query, browser_manager, llm)

    async def _scan_direct(
        self, query: ScanQuery, browser_manager: object
    ) -> ScanResult:
        """Fast scan using direct CDP page control.

        Args:
            query: Search parameters.
            browser_manager: BrowserManager with get_page() method.

        Returns:
            ScanResult with parsed listings.
        """
        scanner = DirectScanner(
            max_listings=self._max_listings,
        )
        return await scanner.scan(query, browser_manager, llm=self._json_llm)

    async def _scan_agent(
        self, query: ScanQuery, browser_manager: object, llm: BaseChatModel
    ) -> ScanResult:
        """Legacy scan using browser-use LLM Agent.

        Args:
            query: Search parameters.
            browser_manager: BrowserManager for creating agents.
            llm: Chat model for the browser-use agent.

        Returns:
            ScanResult with parsed listings and any errors.
        """
        start_time = time.monotonic()
        errors: list[str] = []

        prompt = build_search_prompt(
            keywords=query.keywords,
            max_price=query.max_price,
            location=query.location,
            email=self._email,
            password=self._password,
            max_listings=self._max_listings,
            category=query.category,
        )

        try:
            agent = browser_manager.create_agent(task=prompt, llm=llm)
            history = await agent.run()

            all_content = history.extracted_content()
            raw_result = "\n\n".join(all_content) if all_content else ""

            if not raw_result:
                errors.append("Agent did not complete successfully")
                log.warning("Facebook scan agent failed", keywords=query.keywords)
                return ScanResult(
                    listings=[],
                    errors=errors,
                    scan_duration_seconds=time.monotonic() - start_time,
                )

            log.debug(
                "Raw agent output assembled",
                fragments=len(all_content),
                total_chars=len(raw_result),
            )

            listings = parse_listings(raw_result, site=self.site_name)

        except Exception as exc:
            errors.append(f"Scan failed: {exc}")
            log.error("Facebook scan error", error=str(exc), keywords=query.keywords)
            listings = []

        duration = time.monotonic() - start_time
        log.info(
            "Facebook agent scan complete",
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

        Uses direct CDP navigation when in direct mode, falls back to
        browser-use agent.

        Args:
            listing_url: Direct URL to the Facebook Marketplace listing.
            browser_manager: BrowserManager for page access or agent creation.
            llm: Chat model for browser-use agent.

        Returns:
            Listing with all available details.
        """
        if self._scan_mode == "direct":
            result = await self._get_details_direct(listing_url, browser_manager)
            if result.title:
                return result

        return await self._get_details_agent(listing_url, browser_manager, llm)

    async def _get_details_direct(
        self, listing_url: str, browser_manager: object
    ) -> Listing:
        """Get listing details via direct CDP navigation.

        Args:
            listing_url: URL of the listing.
            browser_manager: BrowserManager with get_page() method.

        Returns:
            Listing with extracted details.
        """
        from agentic_scraper.browser.page_actions import extract_page_text, navigate_and_wait
        from agentic_scraper.sites.facebook.extraction_prompts import (
            EXTRACT_LISTING_DETAIL_PROMPT,
        )

        try:
            page = await browser_manager.get_page()
            await navigate_and_wait(page, listing_url)

            if self._json_llm is not None:
                content = await extract_page_text(page)
                if content:
                    prompt = EXTRACT_LISTING_DETAIL_PROMPT.format(page_content=content)
                    import json

                    response = await self._json_llm.ainvoke(prompt)
                    raw_text = (
                        response.content
                        if hasattr(response, "content")
                        else str(response)
                    )
                    data = json.loads(raw_text)
                    return Listing(
                        site=self.site_name,
                        listing_url=listing_url,
                        title=data.get("title", ""),
                        price=data.get("price"),
                        description=data.get("description", ""),
                        location=data.get("location", ""),
                        seller_name=data.get("seller_name", ""),
                        image_urls=data.get("image_urls", []),
                        external_id=data.get("external_id", ""),
                    )
        except Exception as exc:
            log.warning("Direct detail extraction failed", error=str(exc))

        return Listing(site=self.site_name, listing_url=listing_url)

    async def _get_details_agent(
        self, listing_url: str, browser_manager: object, llm: BaseChatModel
    ) -> Listing:
        """Get listing details via browser-use agent (legacy).

        Args:
            listing_url: URL of the listing.
            browser_manager: BrowserManager for creating agents.
            llm: Chat model for the browser-use agent.

        Returns:
            Listing with extracted details.
        """
        prompt = DETAIL_PROMPT.format(listing_url=listing_url)
        agent = browser_manager.create_agent(task=prompt, llm=llm)
        history = await agent.run()

        raw_result = history.final_result()
        if raw_result:
            listings = parse_listings(raw_result, site=self.site_name)
            if listings:
                return listings[0]

        return Listing(site=self.site_name, listing_url=listing_url)
