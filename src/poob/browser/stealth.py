"""Human-like behavior utilities for browser automation stealth."""

from __future__ import annotations

import asyncio
import random


async def random_delay(min_ms: int, max_ms: int) -> None:
    """Sleep for a random duration between min_ms and max_ms milliseconds.

    Args:
        min_ms: Minimum delay in milliseconds.
        max_ms: Maximum delay in milliseconds.
    """
    seconds = random.uniform(min_ms / 1000, max_ms / 1000)
    await asyncio.sleep(seconds)


def random_scroll_pattern(steps: int = 5) -> list[tuple[str, int, int]]:
    """Generate a human-like scroll pattern.

    Returns a list of (direction, pixels, pause_ms) tuples that simulate
    natural browsing behavior - mostly scrolling down with occasional
    small upward corrections.

    Args:
        steps: Number of scroll actions in the pattern.

    Returns:
        List of (direction, pixels, pause_ms) tuples.
    """
    pattern: list[tuple[str, int, int]] = []
    for i in range(steps):
        # 90% down, 10% small up correction (was 80/20 — too much upward wasted)
        # First 3 steps always go down to get past sponsored listings quickly
        if i < 3 or random.random() < 0.9:
            direction = "down"
            # Larger scroll jumps to cover more ground: 400-1000px
            pixels = random.randint(400, 1000)
        else:
            direction = "up"
            pixels = random.randint(50, 200)  # Small corrections only

        # Longer pauses between scrolls to let Facebook's lazy-load fire
        # Facebook triggers new batch fetch when scrolled past ~80% of content
        pause_ms = random.randint(800, 2500)
        pattern.append((direction, pixels, pause_ms))
    return pattern


async def apply_scroll_pattern(page: object, steps: int = 5) -> None:
    """Execute a human-like scroll pattern on a browser-use Page.

    Generates a random scroll pattern and applies it using page.evaluate()
    with JavaScript window.scrollBy() calls, pausing between each step.

    Args:
        page: A browser-use Page instance with evaluate() method.
        steps: Number of scroll actions.
    """
    from poob.browser.page_actions import scroll_page

    pattern = random_scroll_pattern(steps)
    await scroll_page(page, pattern)


async def scroll_until_stable(
    page: object,
    *,
    max_scrolls: int = 30,
    stable_checks: int = 3,
    scroll_pixels: int = 800,
    pause_ms_min: int = 1200,
    pause_ms_max: int = 3000,
) -> int:
    """Scroll down until no new content loads (infinite scroll exhaustion).

    Keeps scrolling and checking if the page height increases. When the page
    height stops increasing for `stable_checks` consecutive scrolls, we know
    Facebook has no more listings to lazy-load.

    This is how Facebook Marketplace infinite scroll works:
    - Initial load: ~24 listings
    - Each scroll past 80% triggers a GraphQL fetch for ~24 more
    - Eventually the feed runs out and scrollHeight plateaus

    A small upward jitter is inserted every ~5 scrolls to look human.

    Args:
        page: A browser-use Page instance.
        max_scrolls: Safety cap on total scroll actions.
        stable_checks: Stop after this many scrolls with no height change.
        scroll_pixels: Base pixels to scroll per step.
        pause_ms_min: Min pause between scrolls (ms).
        pause_ms_max: Max pause between scrolls (ms).

    Returns:
        Total number of scroll actions performed.
    """
    from poob.browser.page_actions import scroll_page

    last_height = 0
    stable_count = 0
    total_scrolls = 0

    for i in range(max_scrolls):
        # Occasional small upward jitter (every ~5-7 scrolls)
        if i > 0 and i % random.randint(5, 7) == 0:
            jitter = [("up", random.randint(50, 150), random.randint(300, 600))]
            await scroll_page(page, jitter)
            total_scrolls += 1

        # Main downward scroll with randomized distance
        pixels = scroll_pixels + random.randint(-200, 200)
        pattern = [("down", max(pixels, 300), random.randint(pause_ms_min, pause_ms_max))]
        await scroll_page(page, pattern)
        total_scrolls += 1

        # Check current page height
        try:
            raw_height = await page.evaluate(
                "() => document.documentElement.scrollHeight"
            )
            current_height = int(raw_height) if raw_height else 0
        except (Exception, ValueError, TypeError):
            break

        if current_height <= last_height:
            stable_count += 1
            if stable_count >= stable_checks:
                break
        else:
            stable_count = 0
            last_height = current_height

    return total_scrolls


async def simulate_mouse_movement(page: object, movements: int = 2) -> None:
    """Simulate random mouse movements to appear human.

    Moves the mouse to random positions within the viewport to generate
    realistic mousemove events that Facebook's telemetry tracks.

    Args:
        page: A browser-use Page instance.
        movements: Number of mouse movements to simulate.
    """
    try:
        for _ in range(movements):
            x = random.randint(100, 1000)
            y = random.randint(100, 700)
            # Use page.evaluate for mouse movement via JS
            await page.evaluate(
                f"() => {{ "
                f"  const evt = new MouseEvent('mousemove', "
                f"    {{clientX: {x}, clientY: {y}, bubbles: true}});"
                f"  document.dispatchEvent(evt);"
                f" }}"
            )
            await random_delay(200, 600)
    except Exception:
        pass  # Non-critical, don't break the flow


def add_jitter(seconds: float, pct: float = 0.15) -> float:
    """Add random jitter to a target duration.

    Args:
        seconds: Target duration in seconds.
        pct: Maximum jitter as a fraction (0.15 = +/- 15%).

    Returns:
        Duration with jitter applied.
    """
    if pct == 0.0:
        return seconds
    jitter = seconds * random.uniform(-pct, pct)
    return seconds + jitter
