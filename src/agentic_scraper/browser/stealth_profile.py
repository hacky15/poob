"""Stealth profile for browser automation anti-detection.

Provides viewport randomization and JavaScript stealth scripts to reduce
the likelihood of Facebook detecting automated browsing.
"""

from __future__ import annotations

import random

from agentic_scraper.utils.logging import get_logger

log = get_logger("browser.stealth_profile")

# Common real-world screen resolutions to randomize viewport
_COMMON_VIEWPORTS = [
    (1366, 768),   # Most common laptop
    (1440, 900),   # MacBook Air
    (1536, 864),   # Common Windows
    (1920, 1080),  # Full HD
    (1280, 720),   # HD
    (1600, 900),   # Common widescreen
]


def random_viewport() -> dict[str, int]:
    """Pick a random viewport from common resolutions with slight jitter.

    Returns a dict with 'width' and 'height' keys suitable for BrowserProfile.
    Adds ±10px jitter to avoid exact-match fingerprinting.

    Returns:
        Dict with 'width' and 'height' keys.
    """
    base_w, base_h = random.choice(_COMMON_VIEWPORTS)
    w = base_w + random.randint(-10, 10)
    h = base_h + random.randint(-10, 10)
    return {"width": w, "height": h}


# JavaScript stealth scripts to inject before page load.
# Each script patches a known automation detection vector.
STEALTH_SCRIPTS: list[str] = [
    # 1. Remove navigator.webdriver flag (set by automation frameworks)
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})",

    # 2. Fix chrome.runtime (missing in automated Chrome)
    "if (!window.chrome) { window.chrome = {}; }"
    " if (!window.chrome.runtime) { window.chrome.runtime = {}; }",

    # 3. Override navigator.plugins to look like a normal browser
    "Object.defineProperty(navigator, 'plugins', {"
    "  get: () => [1, 2, 3, 4, 5]"
    "})",

    # 4. Override navigator.languages to a standard value
    "Object.defineProperty(navigator, 'languages', {"
    "  get: () => ['en-US', 'en']"
    "})",

    # 5. Fix permissions.query for notifications (automated browsers respond differently)
    "if (navigator.permissions && navigator.permissions.query) {"
    "  const originalQuery = navigator.permissions.query.bind(navigator.permissions);"
    "  navigator.permissions.query = (parameters) => ("
    "    parameters.name === 'notifications'"
    "      ? Promise.resolve({ state: Notification.permission })"
    "      : originalQuery(parameters)"
    "  );"
    "}",
]


async def apply_stealth_scripts(page: object) -> None:
    """Inject stealth JavaScript into a page to hide automation indicators.

    Each script is injected independently so a failure in one does not
    prevent others from being applied. Failures are logged at debug level.

    Args:
        page: A browser-use Page instance with evaluate() method.
    """
    applied = 0
    for script in STEALTH_SCRIPTS:
        try:
            await page.evaluate(f"() => {{ {script} }}")
            applied += 1
        except Exception as exc:
            log.debug("Stealth script injection failed", error=str(exc))

    log.debug("Stealth scripts applied", count=applied, total=len(STEALTH_SCRIPTS))
