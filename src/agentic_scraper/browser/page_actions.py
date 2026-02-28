"""Direct browser page actions using CDP Page object.

Provides async utility functions for navigating, scrolling, and
extracting content from pages without an LLM agent. Uses the
browser-use Page API (CDP-based) directly.
"""

from __future__ import annotations

from agentic_scraper.browser.stealth import random_delay


async def navigate_and_wait(page: object, url: str, wait_ms: int = 3000) -> None:
    """Navigate to URL and wait for content to settle.

    Args:
        page: A browser-use Page instance with goto() method.
        url: The URL to navigate to.
        wait_ms: Minimum wait time in milliseconds after navigation.
    """
    await page.goto(url)
    await random_delay(wait_ms, wait_ms + 2000)


async def scroll_page(page: object, pattern: list[tuple[str, int, int]]) -> None:
    """Execute a scroll pattern on the page via JS evaluation.

    Args:
        page: A browser-use Page instance with evaluate() method.
        pattern: List of (direction, pixels, pause_ms) tuples from
                 stealth.random_scroll_pattern().
    """
    for direction, pixels, pause_ms in pattern:
        delta = pixels if direction == "down" else -pixels
        await page.evaluate(f"() => {{ window.scrollBy(0, {delta}); }}")
        await random_delay(pause_ms // 2, pause_ms)


async def extract_page_text(page: object) -> str:
    """Extract clean markdown text from the current page.

    Args:
        page: A browser-use Page instance with _extract_clean_markdown() method.

    Returns:
        Clean markdown representation of the page content.
    """
    content, _stats = await page._extract_clean_markdown()
    return content


async def evaluate_js(page: object, script: str) -> str:
    """Run JavaScript on the page and return the result string.

    Args:
        page: A browser-use Page instance with evaluate() method.
        script: JavaScript arrow function to execute (must start with `() =>`).

    Returns:
        String result of the JavaScript evaluation.
    """
    return await page.evaluate(script)


async def take_screenshot(page: object) -> str:
    """Take a screenshot and return base64 data.

    Args:
        page: A browser-use Page instance with screenshot() method.

    Returns:
        Base64-encoded PNG screenshot data.
    """
    return await page.screenshot()
