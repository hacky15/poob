---
type: decision
status: active
date: 2026-05-29
tags: [scanner, browser, authentication, freshness, legality, facebook]
related: [[main-browser-headful-in-headless-container]] [[anon-browser-cdp-death-no-recovery]] [[listing-freshness-verification]]
---

# Authenticated FB session, headless, no ongoing human intervention

## Context

Coverage audit (2026-05-29) showed FB's *anonymous* feed serves mostly hours-old listings — only 1 of 52 timestamped listings was <10 min old. The strict 10-min "just listed" notification gate therefore catches almost nothing on the anon path. The operator chose the **authenticated browser** (which sees a fresher feed) to resolve this, with two hard requirements: **no human intervention** and **sustain full functionality** (don't break the working anon pipeline).

The authenticated tier had never actually worked: the main browser was headful in a displayless container (fixed in [[main-browser-headful-in-headless-container]]), there was no login code (`adapter.login()` is a no-op), and no session existed.

## Legality posture (flagged + accepted)

Authenticated FB Marketplace access touches the operator's standing rules (no scraping behind login walls / no ToS violations / no anti-bot bypass). This was explicitly flagged and the operator confirmed proceeding. The non-negotiable line we hold:

- **We NEVER solve a CAPTCHA / checkpoint programmatically.** That is anti-bot bypass. On a checkpoint we STOP, log, and fall back to the anonymous path.

## Decision (revised — cookie import)

The first approach was a headless credential login from the residential IP. Prod proved two things (logged via `FB auth signals`):
- **FB does NOT checkpoint the headless residential login** — no CAPTCHA ever fired. The big risk was unfounded.
- **But the automated credential *submit* does not establish a session** — post-submit the page stays on `/login/` with the form present. FB hardens the login submit against automation specifically.

So automating login is brittle and FB-hostile (and repeatedly POSTing credentials risks flagging the account). The operator chose the reliable path: **one-time cookie import.**

1. **Cookie import.** The operator exports their real FB session cookies once (a ~2-min local browser task, per [[facebook-cookie-import]]) into `browser_profiles/facebook/cookies.json`. On startup, if not already logged in, the patrol browser injects them via CDP `Network.setCookies` and re-checks. Cookies persist in the `poob_poob-browser` volume, so the import lasts weeks — no ongoing human touch.
2. **No automated credential login.** Dropped entirely (didn't work; risked flagging).
3. **Checkpoint detection → fall back, never solve.** Unchanged and non-negotiable.
4. **Module:** `src/poob/sites/facebook/auth.py` — `ensure_logged_in(page, *, cookies_path, load_cookies)` with pure, unit-tested `classify_auth_state` + `parse_cookie_export` (accepts Cookie-Editor list or Playwright storage_state). `BrowserManager.load_cookies` injects via CDP. Wired into `main.py` startup behind `patrol_authenticated_login_enabled` (default True), timeout-guarded.

The patrol engine already prefers the main browser for the DOM sweep + enrichment; once its session holds the imported cookies, that path is authenticated automatically. Anon browser + anon GQL remain the fallback tier (functionality sustained).

## The one honest caveat

"Zero ongoing human intervention" holds: the cookie import is a one-time setup that lasts weeks. It is **not** literally zero-touch forever — FB sessions expire, so re-export is needed occasionally (rare, local, ~2 min, not server/noVNC). When a session is absent/expired the scanner falls back to anon automatically (no breakage). We never solve a CAPTCHA programmatically.

## Validation

- `test_facebook_auth.py` (13): `classify_auth_state` (checkpoint-wins, logged-in, login-form-inconclusive, non-dict); `ensure_logged_in` orchestration (cookie reuse skips login, checkpoint-not-solved on home + post-login, no-credentials, login-success, login-failed).
- Post-deploy: watch `FB authentication result status=...`. `logged_in` => authenticated feed live. `checkpoint`/`failed` => stays anon (functionality intact), and the empirical answer to "does residential headless login checkpoint" is in the logs.

## Follow-ups

- **Session-health re-auth:** if a session expires mid-run, re-verify + re-login (gated, no-solve) at cycle start rather than only at boot. Deferred — startup login is the core; expiry is weeks-scale.
- **Main-browser CDP self-heal:** if the bubus race bites the (now headless) main browser like it did the anon browser, port the `_restart_*` self-heal. Watch the logs first — the headless start has been clean so far.
- **Memory:** two headless Chromium instances in a 4 GiB cap; observed ~1.4 GiB, comfortable, but watch under load.
