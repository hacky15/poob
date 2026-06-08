---
type: architecture
status: active
date: 2026-03-29
tags: [scanner, filter]
related: [[unified-filter-pipeline]] [[detail-enrichment-empty-og-jsonld]]
---

# Garbage listing filter — UI artifacts + unenrichable placeholders

## Purpose

Keep Facebook UI text and unenrichable placeholder "titles" from reaching VLM evaluation. When DOM extraction misfires or detail enrichment fails, these survive all the way downstream without a filter.

## Categories

1. **UI artifacts**: "See details", "Loading", "Facebook Marketplace" — Facebook's own UI text, not listing titles.
2. **Freshness badges**: "Just listed", "Listed today", "" (empty) — DOM scraper captured the freshness indicator instead of the actual title.
3. **Partial extraction**: very short fragments from CSS-clipped text.
4. **Non-item sale events**: "Rummage/Garage/Estate/Yard sale", "FREE AT CURB", "everything must go" — sale *events*, not products. Matched by `_SALE_EVENT_RE` (word-boundary). The ambiguous "moving sale" / "multi-family" / "neighborhood sale" are **deliberately excluded** — they attach to legit single-item titles and are the only path that could suppress a real watchlist DM. See [[public-incredible-selectivity-floors]].

## Behavior

`GarbageFilter` in [listing_filter.py](../../src/poob/scanner/listing_filter.py):

- **UI artifacts**: always removed (no recovery possible).
- **Non-item sale events**: always removed (a sale-event title is structurally not a product; watchlist-safe because a real watched item is never a bare sale-event title).
- **Unenrichable placeholders**: removed only if description is also < 20 chars. If description is present, VLM can still reason from description + image.

## Why not filter earlier

These listings are NOT filtered pre-enrichment. Detail enrichment is designed to fix them — visit the listing page, extract the real title. Only after enrichment has had its chance and failed do we discard.

## Invariants

- Don't preemptively filter anything recoverable by detail enrichment — that path is still the primary mitigation for bad DOM extraction.
- If a new category of garbage appears, add it as a named constant in `GarbageFilter`, not as a sprinkling of `if "..." in title` checks elsewhere.
