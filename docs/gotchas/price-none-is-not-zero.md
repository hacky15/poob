---
type: gotcha
status: active
date: 2026-03-05
tags: [scanner, pricing, llm]
related: [[vlm-triage-pipeline]] [[triage-vlm-context-inputs]]
---

# `listing.price = None` is NOT `$0`

## Trigger

Any code that compares, displays, or calculates on `listing.price`.

## Why it happens

A missing price means our scraper couldn't extract it. It does NOT mean the item is free. Prior code used `listing.price or 0` to coalesce None to 0, then `(MSRP - $0) / MSRP = 100%` → every unknown-price listing looked like an "incredible deal." Caused 29 fake deal notifications in a single patrol cycle.

## Don't

- `listing.price or 0.0`
- `listing.price or 0`
- `if listing.price:` — treats 0.0 (genuinely free) as falsy
- Show "FREE" in any LLM-facing prompt when price is unknown — the VLM will hallucinate a 100% discount

## Do

- `listing.price is not None` checks, always.
- Distinguish three states:
  - `listing.price = 0` with `is not None` → genuinely free, 100% discount is correct.
  - `listing.price = None` → unknown, let VLM assess, skip programmatic enforcement.
  - `listing.price > 0` → known price, normal path.
- Skip `_enforce_dollar_savings()` when price is unknown. VLM's assessment stands on its own, capped at GREAT max.
- Use the three-way display logic for any LLM-facing price rendering (see [[triage-vlm-context-inputs]]).

## Reference

- [[vlm-triage-pipeline]] — full pipeline context
- The "FREE for unknown price" bug recurred in both triage AND VLM prompts independently; fixed in `_build_listings_block()` and the VLM prompt builder with the same three-way helper.
