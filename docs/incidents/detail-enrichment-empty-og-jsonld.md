---
type: incident
status: resolved
date: 2026-03-17
tags: [marketplace, scanner, facebook, extraction]
related: [[facebook-scrolling-and-listing-volume]] [[facebook-og-jsonld-are-dead]]
---

# Detail enrichment returned empty data on every listing

## Symptom

All 37 listings in a patrol run had `og={}` and `ld={}` after detail enrichment. Zero descriptions, zero prices recovered from JSON-LD, zero timestamps from `datePosted`. Run after run, the metrics read `enriched=0 failed=0` — nothing was even being attempted.

Downstream impact: triage and VLM ran blind on text (no descriptions). 25/37 listings had no price because JSON-LD was supposed to fill missing GraphQL prices. 32/37 had no timestamp. "Just listed" garbage titles survived because OG titles didn't exist.

## Root cause

Two independent bugs:

**1. OG / JSON-LD don't exist on Facebook Marketplace for regular browser user agents.**

- OG meta tags are only served to crawler user agents (Googlebot, Facebookbot). Regular browser UAs get an empty shell.
- JSON-LD (`script[type="application/ld+json"]`) **does not exist** on Facebook pages at all — Facebook uses Open Graph protocol, not schema.org. No such script tag will ever appear.
- All listing data flows through GraphQL queries fired by the client JS. The initial HTML is a minimal shell: resource preloads, module definitions, CSS links.

**2. `browser-use`'s `Page.evaluate()` returns JSON strings, not Python objects.**

`Page.evaluate()` returns `json.dumps(value)` for dicts/lists and `""` for None. Every `page.evaluate()` call was receiving a JSON string like `"[]"` or `'{"title": "..."}'` instead of a Python list/dict. The code then treated these strings as truthy/falsy or tried `.get()` on them, which silently failed inside `except Exception: pass` blocks.

## Fix

**Replaced OG/JSON-LD extraction with a three-tier data-sjs strategy:**

1. **Tier 1 — `data-sjs` script tag parsing** (PRIMARY, WORKING): extracts title, description, price, `creation_time`, condition, seller, photos, GPS from `<script type="application/json" data-sjs>` ScheduledServerJS payloads.
2. **Tier 2 — DOM structural selectors**: fills gaps (price text, location, freshness badge).
3. **Tier 3 — page markdown text parsing**: last resort for freshness + seller info.

**Added a `_parse_evaluate_result()` wrapper** that `json.loads()` the string returned by `page.evaluate()` back to a Python object. Applied to all `page.evaluate()` calls in `detail_extractor.py`. Later promoted to a shared utility in `src/poob/utils/content.py`.

**data-sjs timing:** Facebook detail pages load in stages. The initial implementation extracted at 3s and found nothing because `data-sjs` tags were still empty. Fix: poll loop checking every 1s for up to 6s total (2s nav wait + 4 × 1s), break early when marketplace data is found. Diagnostic logging added for the case where tags exist but don't contain marketplace keywords.

**Early bail:** If the first 5 consecutive listings return no new data, stop enrichment and log a warning. Caps wasted time at ~45s instead of 7 minutes.

## Validation

First fixed run: `enriched=8/9`, `with_description=6/13` (was `enriched=0` for weeks).

## Follow-ups

- Separate incident: [[graphql-amount-with-offset-cents-bug]] — price field format confusion surfaced by the same audit.
- Gotcha written: [[facebook-og-jsonld-are-dead]].
- CDP Network interception of `/api/graphql/` was investigated; confirmed NOT the active extraction path because detail data lives in data-sjs, not XHR.
