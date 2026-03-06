"""Web search providers with cascade fallback.

Supports Tavily and Serper.dev, with automatic fallback when one
provider's quota is exhausted.

Tavily free tier: 1,000 searches/month.
Serper.dev free tier: 2,500 searches (one-time credit).
"""

from __future__ import annotations

from typing import Protocol

import httpx

from agentic_scraper.skills.search_throttle import SearchRateLimiter
from agentic_scraper.utils.logging import get_logger

log = get_logger("skills.web_search")

# API endpoints
TAVILY_API_URL = "https://api.tavily.com/search"
SERPER_API_URL = "https://google.serper.dev/search"


class SearchProvider(Protocol):
    """Protocol for web search providers."""

    async def search(self, query: str, include_domains: list[str] | None = None) -> str:
        """Run a web search and return concatenated text snippets."""
        ...

    async def search_results(
        self, query: str, include_domains: list[str] | None = None
    ) -> list[dict]:
        """Run a web search and return raw result dicts."""
        ...


class TavilySearchProvider:
    """Web search via Tavily API.

    Returns clean text snippets from search results — no HTML parsing needed.

    Args:
        api_key: Tavily API key (get free at https://tavily.com).
        rate_limiter: Optional shared rate limiter.
        max_results: Maximum results per query.
        timeout_seconds: HTTP request timeout.
    """

    name = "tavily"

    def __init__(
        self,
        api_key: str,
        rate_limiter: SearchRateLimiter | None = None,
        max_results: int = 5,
        timeout_seconds: int = 15,
    ) -> None:
        self._api_key = api_key
        self._rate_limiter = rate_limiter
        self._max_results = max_results
        self._timeout = timeout_seconds
        self._quota_exhausted = False

    async def search(self, query: str, include_domains: list[str] | None = None) -> str:
        """Run a web search and return concatenated text snippets."""
        if self._quota_exhausted:
            return ""

        if self._rate_limiter:
            await self._rate_limiter.acquire()

        payload: dict = {
            "api_key": self._api_key,
            "query": query,
            "max_results": self._max_results,
            "include_answer": False,
            "search_depth": "basic",
        }
        if include_domains:
            payload["include_domains"] = include_domains

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(TAVILY_API_URL, json=payload)

            if response.status_code != 200:
                body = ""
                try:
                    body = response.text[:300]
                except Exception:
                    pass
                log.warning(
                    "Tavily search failed",
                    status=response.status_code,
                    query=query[:60],
                    response_body=body,
                )
                # Mark quota exhausted on 432 so we skip future calls this session
                if response.status_code == 432:
                    self._quota_exhausted = True
                return ""

            data = response.json()
            results = data.get("results", [])

            parts: list[str] = []
            for r in results:
                title = r.get("title", "")
                content = r.get("content", "")
                url = r.get("url", "")
                if title or content:
                    parts.append(f"{title}\n{content}\n{url}")

            text = "\n\n".join(parts)
            log.info(
                "Tavily search complete",
                query=query[:60],
                results=len(results),
                text_len=len(text),
            )
            return text

        except Exception as exc:
            log.warning("Tavily search error", error=str(exc), query=query[:60])
            return ""

    async def search_results(
        self, query: str, include_domains: list[str] | None = None
    ) -> list[dict]:
        """Run a web search and return raw result dicts."""
        if self._quota_exhausted:
            return []

        if self._rate_limiter:
            await self._rate_limiter.acquire()

        payload: dict = {
            "api_key": self._api_key,
            "query": query,
            "max_results": self._max_results,
            "include_answer": False,
            "search_depth": "basic",
        }
        if include_domains:
            payload["include_domains"] = include_domains

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(TAVILY_API_URL, json=payload)

            if response.status_code != 200:
                if response.status_code == 432:
                    self._quota_exhausted = True
                log.warning("Tavily search failed", status=response.status_code)
                return []

            data = response.json()
            return data.get("results", [])

        except Exception as exc:
            log.warning("Tavily search_results error", error=str(exc))
            return []


class SerperSearchProvider:
    """Web search via Serper.dev (Google Search API).

    Free tier: 2,500 searches (one-time credit, no CC required).
    Returns Google SERP results with title, snippet, link.

    Args:
        api_key: Serper.dev API key (get free at https://serper.dev).
        rate_limiter: Optional shared rate limiter.
        max_results: Maximum results per query.
        timeout_seconds: HTTP request timeout.
    """

    name = "serper"

    def __init__(
        self,
        api_key: str,
        rate_limiter: SearchRateLimiter | None = None,
        max_results: int = 5,
        timeout_seconds: int = 15,
    ) -> None:
        self._api_key = api_key
        self._rate_limiter = rate_limiter
        self._max_results = max_results
        self._timeout = timeout_seconds
        self._quota_exhausted = False

    async def search(self, query: str, include_domains: list[str] | None = None) -> str:
        """Run a web search and return concatenated text snippets."""
        if self._quota_exhausted:
            return ""

        if self._rate_limiter:
            await self._rate_limiter.acquire()

        payload: dict = {
            "q": query,
            "num": self._max_results,
        }
        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    SERPER_API_URL, json=payload, headers=headers
                )

            if response.status_code != 200:
                body = ""
                try:
                    body = response.text[:300]
                except Exception:
                    pass
                log.warning(
                    "Serper search failed",
                    status=response.status_code,
                    query=query[:60],
                    response_body=body,
                )
                if response.status_code in (401, 403, 429):
                    self._quota_exhausted = True
                return ""

            data = response.json()
            organic = data.get("organic", [])

            parts: list[str] = []
            for r in organic:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                link = r.get("link", "")
                if title or snippet:
                    parts.append(f"{title}\n{snippet}\n{link}")

            text = "\n\n".join(parts)
            log.info(
                "Serper search complete",
                query=query[:60],
                results=len(organic),
                text_len=len(text),
            )
            return text

        except Exception as exc:
            log.warning("Serper search error", error=str(exc), query=query[:60])
            return ""

    async def search_results(
        self, query: str, include_domains: list[str] | None = None
    ) -> list[dict]:
        """Run a web search and return raw result dicts."""
        if self._quota_exhausted:
            return []

        if self._rate_limiter:
            await self._rate_limiter.acquire()

        payload: dict = {
            "q": query,
            "num": self._max_results,
        }
        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    SERPER_API_URL, json=payload, headers=headers
                )

            if response.status_code != 200:
                if response.status_code in (401, 403, 429):
                    self._quota_exhausted = True
                log.warning("Serper search failed", status=response.status_code)
                return []

            data = response.json()
            organic = data.get("organic", [])

            # Normalize to same format as Tavily (title, content, url)
            return [
                {
                    "title": r.get("title", ""),
                    "content": r.get("snippet", ""),
                    "url": r.get("link", ""),
                }
                for r in organic
            ]

        except Exception as exc:
            log.warning("Serper search_results error", error=str(exc))
            return []


class SearchProviderCascade:
    """Cascade of search providers — tries each in order until one succeeds.

    When a provider returns empty results due to quota exhaustion (HTTP 432/429),
    it automatically falls through to the next provider. Providers that hit quota
    limits are marked as exhausted for the session.

    Args:
        providers: Ordered list of search providers (first = primary).
    """

    def __init__(self, providers: list[TavilySearchProvider | SerperSearchProvider]) -> None:
        self._providers = providers

    async def search(self, query: str, include_domains: list[str] | None = None) -> str:
        """Try each provider in order until one returns results."""
        for provider in self._providers:
            result = await provider.search(query, include_domains)
            if result:
                return result
        return ""

    async def search_results(
        self, query: str, include_domains: list[str] | None = None
    ) -> list[dict]:
        """Try each provider in order until one returns results."""
        for provider in self._providers:
            results = await provider.search_results(query, include_domains)
            if results:
                return results
        return []
