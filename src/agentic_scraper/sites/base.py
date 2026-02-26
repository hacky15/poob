"""Base protocol and data structures for site adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agentic_scraper.storage.models import Listing

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


@dataclass(frozen=True)
class ScanQuery:
    """Parameters for a marketplace search."""

    keywords: str
    max_price: float | None = None
    location: str | None = None
    radius_miles: int | None = None
    category: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """Result of a site scan operation."""

    listings: list[Listing] = field(default_factory=list)
    raw_page_content: str | None = None
    screenshot_b64: str | None = None
    errors: list[str] = field(default_factory=list)
    scan_duration_seconds: float = 0.0


@runtime_checkable
class SiteAdapter(Protocol):
    """Protocol for pluggable marketplace site adapters.

    Implement this protocol to add support for a new marketplace site.
    Each adapter handles navigation, search, and listing extraction
    for a specific platform.
    """

    @property
    def site_name(self) -> str:
        """Unique identifier for this site (e.g. 'facebook_marketplace')."""
        ...

    @property
    def base_url(self) -> str:
        """Base URL for the marketplace (e.g. 'https://facebook.com/marketplace')."""
        ...

    @property
    def requires_login(self) -> bool:
        """Whether this site requires authentication to browse listings."""
        ...

    async def login(self, browser_manager: object) -> bool:
        """Authenticate with the site using the managed browser.

        Args:
            browser_manager: BrowserManager instance for browser control.

        Returns:
            True if login succeeded or was already logged in.
        """
        ...

    async def scan(
        self, query: ScanQuery, browser_manager: object, llm: BaseChatModel
    ) -> ScanResult:
        """Search the site and extract listings.

        Args:
            query: Search parameters (keywords, price, location).
            browser_manager: BrowserManager for agent creation.
            llm: LangChain chat model for the browser-use agent.

        Returns:
            ScanResult with extracted listings and any errors.
        """
        ...

    async def get_listing_details(
        self, listing_url: str, browser_manager: object, llm: BaseChatModel
    ) -> Listing:
        """Navigate to a single listing and extract full details.

        Args:
            listing_url: Direct URL to the listing page.
            browser_manager: BrowserManager for agent creation.
            llm: LangChain chat model for the browser-use agent.

        Returns:
            Listing with all available details populated.
        """
        ...
