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

## Decision

Establish an authenticated session with the minimum CAPTCHA exposure and zero ongoing human touch:

1. **Cookie reuse first.** The `browser_profiles` Docker volume persists across deploys. On startup we load FB and check for a valid session; if present, we reuse it with **no login event** (the login event is the #1 checkpoint trigger). A successful session lasts weeks, so logins are rare.
2. **One-time headless login** with the `.env` credentials only when no valid session exists. The outbound IP is residential (Madison AT&T, matches the account's normal location), which keeps checkpoint risk low.
3. **Checkpoint detection → fall back, never solve.** If FB shows a checkpoint/2FA/CAPTCHA (on home or post-login), we return `CHECKPOINT`, log it, and the patrol continues on the anonymous path.
4. **Module:** `src/poob/sites/facebook/auth.py` — `ensure_logged_in(page, email, password) -> AuthStatus` with a pure, unit-tested `classify_auth_state(signals)`. Wired into `main.py` startup after the main browser starts, behind `patrol_authenticated_login_enabled` (default True), guarded by timeouts so a hang can't block boot.

The patrol engine already prefers the main browser for the DOM sweep + detail-page enrichment; once its profile holds a logged-in session, that path is authenticated automatically. The anonymous browser + anon GQL remain as the fallback tier (functionality sustained).

## The one honest caveat

Whether FB checkpoints a *programmatic* login is empirical — the residential IP makes it likely-OK but not guaranteed. If it ever checkpoints:
- The system falls back to anon automatically (no break).
- Re-establishing auth would need a **one-time cookie refresh on a trusted machine** (local, ~2 min, not server/noVNC) — rare, because a session lasts weeks once established. This is the only residual human touch, and it does not violate the no-anti-bot-bypass rule.

So "zero human intervention" holds for the steady state and the happy-path first login; it cannot be *guaranteed* forever because authenticating inherently requires credentials/cookies and FB may checkpoint.

## Validation

- `test_facebook_auth.py` (13): `classify_auth_state` (checkpoint-wins, logged-in, login-form-inconclusive, non-dict); `ensure_logged_in` orchestration (cookie reuse skips login, checkpoint-not-solved on home + post-login, no-credentials, login-success, login-failed).
- Post-deploy: watch `FB authentication result status=...`. `logged_in` => authenticated feed live. `checkpoint`/`failed` => stays anon (functionality intact), and the empirical answer to "does residential headless login checkpoint" is in the logs.

## Follow-ups

- **Session-health re-auth:** if a session expires mid-run, re-verify + re-login (gated, no-solve) at cycle start rather than only at boot. Deferred — startup login is the core; expiry is weeks-scale.
- **Main-browser CDP self-heal:** if the bubus race bites the (now headless) main browser like it did the anon browser, port the `_restart_*` self-heal. Watch the logs first — the headless start has been clean so far.
- **Memory:** two headless Chromium instances in a 4 GiB cap; observed ~1.4 GiB, comfortable, but watch under load.
