---
type: reference
status: active
date: 2026-03-15
tags: [browser, stealth, facebook]
source: "Internal research + observed blocks"
related: [[facebook-scrolling-and-listing-volume]]
---

# Browser stealth signals Facebook tracks

## Summary

What the scraping browser must look like to avoid Facebook's bot-detection. Observed, not documented — update whenever a new signal appears in the wild.

## Signals Facebook tracks

- **Viewport size** — randomized via `stealth_viewport_randomize`.
- **Mouse movements** — simulated via `simulate_mouse_movement`.
- **Scroll patterns** — realistic: mostly down, occasional small up-corrections.
- **Request timing** — randomized delays between page navigations.
- **Concurrent tab patterns** — limited to 2 for detail enrichment. 3+ risks detection.

## Shadow-ban detection

3+ consecutive empty sweeps triggers a suspected shadow-ban warning. Tracked by `_consecutive_empty_sweeps` counter in `PatrolEngine`. Reset on any successful sweep.

## Usage in this project

- [browser/manager.py](../../src/poob/browser/manager.py) — stealth options wiring.
- [scanner/patrol_engine.py](../../src/poob/scanner/patrol_engine.py) — `_consecutive_empty_sweeps` tracking.

## Source

Internal observation. If we ever get a hard signal (account block, CAPTCHA escalation), log it here with date and circumstances.
