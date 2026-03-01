"""Compatibility shim - WatchlistMatcher has been renamed to InterestMatcher.

Import from agentic_scraper.scanner.interest_matcher instead.
"""

from agentic_scraper.scanner.interest_matcher import InterestMatcher as WatchlistMatcher

__all__ = ["WatchlistMatcher"]
