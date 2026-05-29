---
type: incident
status: resolved
date: 2026-05-29
tags: [scanner, browser, cdp, headless, authentication, infra]
related: [[anon-browser-cdp-death-no-recovery]] [[browse-path-ignored-configured-location]]
---

# Authenticated main browser dead: headful Chromium in a displayless container

## Symptom

The authenticated "main" browser logged `Main browser CDP marked permanently broken` / `new_page() failed after 6 attempts; CDP client not initialized` on every container, falling back to anon-only forever. The anon browser (separate instance) worked fine. The authenticated tier had effectively never functioned in prod.

## Root cause

`BROWSER_HEADLESS=false` in the deploy env (`/home/ben/apps/stacks/poob/deploy/.env:22`; also shipped `false` in `.env.example`). `main.py` constructs the main `BrowserManager(headless=config.browser_headless)` → **headful**. The container has **no X display**, so headful Chromium cannot launch — the process hangs at launch, the bubus launch handler times out at 30s, the CDP client never connects, and `get_page()` fails permanently.

The anon browser is hardcoded `headless=True` (`main.py:260`), which is the only reason it runs. The differentiator was purely the headless flag: headless launches and (after the bubus race) connects CDP; headful never launches without a display.

Confirmed: prod env `BROWSER_HEADLESS=false`, `DISPLAY` unset in the container. The `_cdp_permanently_broken` short-circuit (added to stop wasting 30s/cycle on retries) was correctly giving up — but the underlying cause was the unsatisfiable headful launch, not a transient CDP race.

## Fix

`BrowserManager.__init__` now auto-detects: if `headless=False` **and** no `DISPLAY` env, force `headless=True` with a warning. A headful Chromium is impossible without a display, so this is strictly correct; local dev with a real display still gets headful when requested. Auto-detecting (rather than just flipping the deploy env) means a Komodo redeploy — which rewrites `deploy/.env` from the stack config — can't silently reintroduce the broken state.

## Validation

- `TestHeadlessAutoDetect`: forces headless when no DISPLAY; respects headful when DISPLAY present; headless=True stays True regardless. Full `test_browser_manager.py` green (16 passed, 1 skipped).
- Post-deploy: the `Main browser CDP marked permanently broken` spam should stop and `Browser started headless=True` should appear for the main browser; `get_page()` should succeed.

## Follow-ups

This resurrects the main browser but it is **unauthenticated** — no login flow exists (`adapter.login()` is a no-op) and the profile has no `c_user`/cookies. Detail-page `data-sjs` is served to logged-out viewers too, so enrichment still works via the now-running main browser, but the *authenticated* (fresher-feed) value requires the next builds:
- Real headless login gated on no-existing-session (operator-approved; cookies persist in the `poob_poob-browser` volume; residential Madison IP lowers checkpoint risk).
- Checkpoint detection → fall back to anon, never solve a CAPTCHA (anti-bot-bypass is forbidden by the operator's standing rules).
- Session-health gate so an unauthenticated/expired main browser doesn't get preferred over the working anon path.
- Watch container memory: two headless Chromium instances (main + anon) in a 4 GiB cap.
