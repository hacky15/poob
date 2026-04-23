---
type: gotcha
status: active
date: 2026-03-17
tags: [marketplace, facebook, extraction]
related: [[detail-enrichment-empty-og-jsonld]] [[facebook-scrolling-and-listing-volume]]
---

# OG tags and JSON-LD do NOT work on Facebook Marketplace

## Trigger

Trying to extract listing metadata from detail pages via OpenGraph meta tags or `<script type="application/ld+json">`.

## Why it happens

- **OG meta tags** are only served to crawler user agents (Googlebot, Facebookbot). Regular browser UAs get an empty shell. `meta[property^="og:"]` returns nothing.
- **JSON-LD** does not exist on Facebook Marketplace pages at all. Facebook uses Open Graph protocol, not schema.org. No `script[type="application/ld+json"]` will ever appear.
- Facebook is a React/Relay SPA — all listing data flows through GraphQL client-side fetches and is embedded in `<script type="application/json" data-sjs>` tags.

## Don't

- Add OG-tag extraction anywhere in the pipeline.
- Add JSON-LD extraction anywhere in the pipeline.
- "Just wait longer" for OG to populate — it never will for browser UAs.

## Do

- Use the three-tier `data-sjs` strategy: data-sjs script tags (primary) → DOM structural selectors (tier 2) → page markdown text parsing (tier 3). See [[detail-enrichment-empty-og-jsonld]] for the incident and [[facebook-scrolling-and-listing-volume]] for where this slots into the pipeline.

## Reference

- `src/poob/sites/facebook/detail_extractor.py` — current extraction path.
- `docs/research/fb-detail-page-extraction.md` — full research writeup (when migrated into Phase 4).
