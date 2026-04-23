---
type: architecture
status: active
date: 2026-03-25
tags: [vlm, triage, llm, pricing, watchlist]
related: [[triage-vlm-context-inputs]] [[vlm-cascade-operational-findings]] [[price-none-is-not-zero]]
---

# VLM & triage pipeline — what each stage does

## Purpose

Evaluate scraped listings for deal quality without spamming users. Text triage culls the obvious garbage cheaply; VLM does the expensive multimodal evaluation only on survivors. Savings thresholds are enforced programmatically — VLMs cannot rate everything "incredible."

## Stages

1. **Text triage (LLM)** — sees title, price, short description, location, seller, condition, watchlist preferences as hard rules. Batched 5 listings per prompt. Does NOT see images, comparable sales, or enrichment data.
2. **Visual enrichment** — OCR on images, brand/model/product identification, condition signals.
3. **VLM deep evaluation** — sees 15+ data points including 2 images, full description, comparable sales, visual enrichment, regex-detected signals, triage flags, watchlist context. Multi-provider voting (3 voters, optional tiebreaker + second opinion). See [[triage-vlm-context-inputs]] for the complete input list.
4. **Programmatic savings enforcement** — `_enforce_dollar_savings()` caps VLM score based on hard dollar + percent thresholds.

## Watchlist bypass

Watchlist-tagged listings (`_watch_item_id` in raw_data) **bypass text triage entirely**. Triage was observed killing 77% of valid watchlist matches — the user explicitly asked for these items, triage is too aggressive.

Preference constraints are not lost:

- Negations ("not metal"), bulk detection ("lot of", "shoebox of"), and positive constraints ("only cups") are enforced programmatically by `_pre_enrichment_preference_filter` after the bypass.
- Still subject to [[unified-filter-pipeline]] (geo, freshness, garbage filters) — bypass is for triage only.

## Price display for LLMs

The same price-display logic MUST be used everywhere an LLM sees price:

```python
if listing.price is not None and listing.price > 0: "$X.XX"
elif listing.price is not None and listing.price == 0: "FREE"
else: "Price not listed" / "Not listed (check listing image for price)"
```

Previously, triage and VLM both showed "FREE" for unknown prices. VLMs hallucinated 100% discounts and the savings enforcer then nodded them through. See [[price-none-is-not-zero]].

## Savings enforcement thresholds

Programmatic overrides — VLM cannot bypass.

| Score | Flat threshold | Applies when |
|---|---|---|
| GOOD | 15%+ discount AND $10+ saved | `listing.price is not None` |
| GREAT | 30%+ discount AND $30+ saved | `listing.price is not None` |
| INCREDIBLE | 50%+ discount AND $75+ saved | `listing.price is not None` |

When price is unknown: VLM score stands but capped at GREAT max.

### Price-scaled dollar thresholds (items under $50)

Flat thresholds prevent "incredible" on cheap junk but over-penalize legitimate budget items. A $20 item at 50% off ($10 saved) is genuinely good but fails the flat $30 GREAT threshold.

For items under $50, dollar thresholds scale proportionally:

- `effective_min = min(flat_threshold, listing_price * pct_factor)`
- GOOD: 15% of listing price (e.g. $3 for a $20 item vs. $10 flat)
- GREAT: 25% of listing price (e.g. $5 for a $20 item vs. $30 flat)
- INCREDIBLE: 40% of listing price (e.g. $8 for a $20 item vs. $75 flat)
- Items $50+ use flat thresholds exclusively.
- Items with unknown price ($0) use flat thresholds (conservative).

So a $15 kitchen item at 40% off ($6 saved) qualifies as GOOD (proportional min $2.25) instead of being demoted to FAIR (flat min $10).

## Invariants

- **`listing.price = None` is NOT `$0`.** See [[price-none-is-not-zero]].
- **Price display is consistent across all LLM-facing code.** Triage, VLM, personality — all use the same three-way logic.
- **Watchlist listings bypass triage by design** — don't re-add triage without measuring the 77% kill rate again.
- **Savings enforcement only runs when `listing.price is not None`.**
- **OG tags and JSON-LD don't exist on Facebook** — see [[facebook-og-jsonld-are-dead]].

## Related

- [[vlm-cascade-operational-findings]] — per-provider performance and cascade order.
- [[triage-vlm-context-inputs]] — exact field list each stage sees.
- [[unified-filter-pipeline]] — filter order and tag-based exemptions.
- [[deal-provenance-trail]] — how evaluation details are stored for notification display.
