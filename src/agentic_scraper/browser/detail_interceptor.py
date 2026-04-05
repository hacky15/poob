"""DEPRECATED — This file is unused and scheduled for removal.

Facebook embeds listing detail data in data-sjs script tags in the initial
HTML, NOT as separate XHR GraphQL calls. The CDP interceptor was built on
the incorrect assumption that detail data arrives via XHR. See Issue #4 in
docs/handoff-audit-april-2026.md for the full explanation.

The actual extraction path is: detail_extractor.py → parse_data_sjs_payloads().

Original description:
CDP network interceptor for Facebook Marketplace detail page GraphQL responses.

Captures the GraphQL response that Facebook sends when navigating to a listing
detail page. This response contains the full listing data: description,
creation_time, all photos, condition, GPS, seller info — fields that are NOT
available in search results.

Uses browser-use's CDP client directly — registers Network event handlers
scoped to a specific page target's session_id. This ensures events fire
correctly even with multiple tabs.

See docs/research/fb-detail-page-extraction.md for background.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from agentic_scraper.sites.facebook.detail_graphql_extractor import (
    DetailPageData,
    _extract_from_payload_text,
    _walk_json_for_listing,
)
from agentic_scraper.utils.logging import get_logger

log = get_logger("browser.detail_interceptor")


class DetailGraphQLInterceptor:
    """Captures GraphQL responses from a browser-use page during navigation.

    Uses CDP Network domain events scoped to a specific page's session_id.

    Usage:
        interceptor = DetailGraphQLInterceptor()
        await interceptor.attach(page)  # Enable Network + register handlers
        # Navigate to detail page...
        detail = await interceptor.wait_for_data(timeout=6.0)
        interceptor.reset()  # Ready for next page
    """

    def __init__(self) -> None:
        self._result: DetailPageData | None = None
        self._data_event: asyncio.Event = asyncio.Event()
        self._pending_requests: dict[str, str] = {}
        self._cdp_client: Any = None
        self._session_id: str | None = None
        self._attached = False

    @property
    def result(self) -> DetailPageData | None:
        """The captured detail page data, or None if nothing captured yet."""
        return self._result

    async def attach(self, page: object) -> bool:
        """Enable Network domain and register event handlers for a page.

        Args:
            page: A browser-use Page object (from BrowserSession.new_page()).

        Returns:
            True if attached successfully, False on failure.
        """
        try:
            # browser-use Page stores: _browser_session, _client (cdp_client),
            # _target_id, _session_id
            self._cdp_client = getattr(page, "_client", None)
            if self._cdp_client is None:
                bs = getattr(page, "_browser_session", None)
                if bs:
                    self._cdp_client = getattr(bs, "cdp_client", None)

            if self._cdp_client is None:
                log.warning("detail_interceptor.no_cdp_client",
                            page_type=type(page).__name__,
                            has_browser_session=hasattr(page, "_browser_session"))
                return False

            # Ensure the page has a CDP session
            session_id = getattr(page, "_session_id", None)
            if not session_id:
                ensure = getattr(page, "_ensure_session", None)
                if ensure:
                    session_id = await ensure()
            self._session_id = session_id

            if not self._session_id:
                log.warning("detail_interceptor.no_session_id",
                            page_type=type(page).__name__)
                return False

            # Enable Network domain for this page's session
            await self._cdp_client.send.Network.enable(
                session_id=self._session_id
            )

            # Register event handlers on the shared cdp_client
            # These fire for ALL sessions — we filter by session_id in the callback
            register = getattr(self._cdp_client, "register", None)
            if register:
                register.Network.requestWillBeSent(self._on_request)
                register.Network.loadingFinished(self._on_loading_finished)
            else:
                log.warning("detail_interceptor.no_register_method")
                return False

            self._attached = True
            log.info(
                "detail_interceptor.attached",
                session_id=self._session_id[:20] if self._session_id else None,
            )
            return True

        except Exception as exc:
            log.warning("detail_interceptor.attach_failed", error=str(exc)[:100])
            return False

    def _on_request(self, params: dict, session_id: str | None) -> None:
        """Track POST requests to Facebook's GraphQL endpoint."""
        if self._result is not None:
            return

        try:
            req = params.get("request", {})
            if isinstance(req, dict):
                url = req.get("url", "")
                method = req.get("method", "")
            else:
                url = getattr(req, "url", "")
                method = getattr(req, "method", "")

            request_id = (
                params.get("requestId")
                if isinstance(params, dict)
                else getattr(params, "requestId", None)
            )

            if not request_id:
                return

            if "/api/graphql" in url and method == "POST":
                # Store the session_id from the event — needed for getResponseBody
                self._pending_requests[request_id] = session_id
        except Exception:
            pass

    def _on_loading_finished(self, params: dict, session_id: str | None) -> None:
        """Fetch the response body for tracked GraphQL requests."""

        try:
            request_id = (
                params.get("requestId")
                if isinstance(params, dict)
                else getattr(params, "requestId", None)
            )

            if not request_id or request_id not in self._pending_requests:
                return

            event_session_id = self._pending_requests.pop(request_id, None)
            asyncio.create_task(self._fetch_and_parse(request_id, event_session_id))
        except Exception:
            pass

    async def _fetch_and_parse(
        self, request_id: str, event_session_id: str | None = None
    ) -> None:
        """Fetch a GraphQL response body and parse detail data from it."""
        if not self._cdp_client or self._result is not None:
            return

        # Use the session_id from the CDP event (matches the page that made
        # the request), falling back to our stored session_id.
        fetch_session = event_session_id or self._session_id

        try:
            resp = await self._cdp_client.send.Network.getResponseBody(
                params={"requestId": request_id},
                session_id=fetch_session,
            )
            body = resp.get("body", "")
            if resp.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", errors="replace")

            body_len = len(body)
            if not body or body_len < 200:
                log.debug("detail_interceptor.body_too_short",
                         request_id=request_id[:20], body_len=body_len)
                return

            detail = self._parse_detail_response(body)
            if detail and (detail.title or detail.description):
                self._result = detail
                self._data_event.set()
                log.info(
                    "detail_interceptor.captured",
                    has_title=bool(detail.title),
                    has_desc=bool(detail.description),
                    desc_len=len(detail.description),
                    has_price=detail.price is not None,
                    has_posted=detail.posted_at is not None,
                )
            else:
                # Log what we found to understand why parsing failed
                has_marketplace = "marketplace" in body[:2000].lower()
                has_listing = "listing_price" in body or "marketplace_listing" in body
                log.debug("detail_interceptor.parse_miss",
                         request_id=request_id[:20],
                         body_len=body_len,
                         has_marketplace_keyword=has_marketplace,
                         has_listing_fields=has_listing,
                         body_preview=body[:150].replace("\n", " "))
        except Exception as exc:
            log.warning("detail_interceptor.fetch_failed",
                        request_id=request_id[:20],
                        error=str(exc)[:120])

    def _parse_detail_response(self, body: str) -> DetailPageData | None:
        """Parse a GraphQL response body into DetailPageData."""
        text = body.strip()
        if text.startswith("for (;;);"):
            text = text[len("for (;;);"):]

        result = DetailPageData()

        # Strategy 1: Regex extraction (resilient to nesting depth)
        _extract_from_payload_text(text, result)

        # Strategy 2: Parse as JSON and walk the structure
        try:
            data = json.loads(text)
            _walk_json_for_listing(data, result)
        except (json.JSONDecodeError, ValueError):
            pass

        if result.title or result.description or result.posted_at:
            return result
        return None

    async def wait_for_data(self, timeout: float = 6.0) -> DetailPageData | None:
        """Wait for a GraphQL response to be captured.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            DetailPageData if captured, None if timeout.
        """
        try:
            await asyncio.wait_for(self._data_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.debug("detail_interceptor.timeout",
                      pending_requests=len(self._pending_requests),
                      attached=self._attached)
        return self._result

    def reset(self) -> None:
        """Clear captured data for the next detail page navigation."""
        self._result = None
        self._data_event.clear()
        self._pending_requests.clear()

    def detach(self, page: object) -> None:
        """Clean up (best-effort — CDP event handlers can't be easily unregistered)."""
        self._pending_requests.clear()
        self._result = None
        self._attached = False
