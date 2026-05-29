---
type: decision
status: active
date: 2026-05-29
tags: [scanner, authentication, freshness, discovery, facebook]
related: [[authenticated-session-no-human]] [[facebook-cookie-import]] [[listing-freshness-verification]]
---

# Route discovery through the authenticated browser, not just enrichment

## Context

After the cookie import established a logged-in session (`status=logged_in`), the first authenticated cycles showed the login alone did NOT make listings fresher:

- `funnel.cycle`: `enriched=11 ts_coverage_pct=100 vlm_evaluated=0` — every enriched listing was >6h old and cut at the freshness filter before VLM.
- DB age-at-scrape of the 12 post-auth listings: **10 were >24h old, 1 was 6–24h, 0 fresh.**

Root cause: authentication powered *enrichment* (detail pages → 100% timestamps), but listing *discovery* still ran entirely through the anonymous tier (anon GQL + anon DOM sweep), which serves mostly stale inventory. The fresher logged-in "newest near you" marketplace feed was never being swept.

## Decision

**Add an authenticated DOM sweep as a preferred discovery source.** When the main browser holds a confirmed session (`BrowserManager.is_authenticated`, set after cookie import), `_sweep_and_intercept` runs `_auth_dom_sweep` — the logged-in browser sweeping the marketplace newest feed — and merges its results FIRST (freshest), ahead of anon GQL and anon DOM. The cycle's `data_source` is reported as `authenticated` when the auth sweep yields listings.

- `BrowserManager.is_authenticated` / `mark_authenticated()`; `main.py` sets it from the cookie-import `AuthStatus`.
- `_auth_dom_sweep` mirrors `_anon_dom_sweep` (per-step 60s timeout, sweep metrics, canary scan) but uses the authenticated main browser.
- Additive + bounded: anon GQL + anon DOM still run and remain the fallback when unauthenticated or when the auth sweep is empty/times out — so functionality is sustained and there's no regression for the unauthenticated path.

## Why this is the right layer

The freshness problem is a *discovery* problem (what FB shows us), not an *extraction* problem (we already get timestamps when we enrich). Fixing it at discovery — sweeping the feed FB actually keeps fresh for logged-in users — is the source-level fix. Enrichment-only auth was treating the symptom.

## The open question — ANSWERED: the logged-in feed is materially fresher

Measured within the hour of deploy (age-at-scrape, distinct listings):

| Age when first seen | Auth-discovery (≥21:03) | Anon baseline (19:00–21:03) |
|---|---|---|
| **<10 min** | **7** | **0** |
| 30–60 min | 1 | 2 |
| 1–6 h | 2 | 13 |
| 6–24 h | 2 | 9 |
| >24 h | 29 | 26 |
| NULL posted_at | 5 (11%) | 80 (62%) |

The logged-in feed surfaced **7 listings <10 min old** (the public-gate band) vs **0** on anon, and NULL-timestamp coverage improved from 62% to 11%. `funnel.cycle` `vlm_evaluated` rose from the chronic 0–2 to **7** in the first auth-discovery cycle. Confirmed: routing discovery through the authenticated browser is what converts the login into genuinely-fresh local listings. The freshness barrier behind near-zero notifications is resolved at the source. (The >24h tail is the broad category-browse backlog; it's correctly filtered out before VLM.)

## Validation

- `TestAuthenticatedDiscoverySweep`: auth sweep runs + is preferred (`source=authenticated`) when authenticated; skipped (anon path, `source=anonymous_graphql`) when not. `mock_browser_manager.is_authenticated=False` by default so existing tests don't accidentally trigger it. Existing `TestSweepAndIntercept` still green (merge restructure caused no regression).
- Post-deploy: `Authenticated browser DOM sweep count=N`, `data_source=authenticated`, and — the real test — the age-at-scrape of `authenticated_dom` listings and whether `vlm_evaluated`/`notified` rise.

## Follow-ups

- If the logged-in feed proves fresher, consider dropping the anon DOM sweep when authenticated (avoid double browser work).
- Multi-center (Madison + Appleton) currently rotates on the GQL browse path; extend the auth DOM sweep's location anchoring similarly if needed.
- Session expiry → re-import cookies (runbook); a session-health re-check per cycle remains a deferred robustness follow-up.
