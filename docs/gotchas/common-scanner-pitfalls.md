---
type: gotcha
status: active
date: 2026-03-20
tags: [scanner, marketplace, facebook, llm]
related: [[vlm-triage-pipeline]] [[price-none-is-not-zero]] [[facebook-og-jsonld-are-dead]] [[graphql-amount-units-cents-vs-dollars]]
---

# Scanner / marketplace pipeline pitfalls

## Trigger

Writing or modifying anything in `src/poob/scanner/`, `src/poob/triage/`, `src/poob/vlm/`, or `src/poob/sites/facebook/`.

## The list

Each item is a hazard that has bitten us before.

- **Never treat `None` as `0` for prices.** Use `is not None` checks. `listing.price or 0.0` caused 29 false deals in one cycle. See [[price-none-is-not-zero]].
- **GraphQL empty responses are normal.** `edges:[]` with `text/html` content-type is "no results", not "blocked". Don't retry or re-bootstrap.
- **Stealth delays are only for browser actions.** GraphQL HTTP requests have their own rate limiting — don't add inter-search delays on top.
- **`page.evaluate()` can return strings.** browser-use returns `json.dumps(value)`. Always coerce numeric results with `int()` or `float()`, or use the shared `parse_evaluate_result()` helper.
- **The VLM can see the price in photos.** Don't assume unknown price = unknown deal quality. The VLM has more context than our scraper.
- **Savings enforcement is the safety net, not the evaluator.** Only runs when `listing.price is not None`. When price is unknown, VLM assessment stands (capped at GREAT).
- **Facebook's sort order is a suggestion.** Always verify with `_verify_sort_order` and expect violations — engagement-promoted listings get injected.
- **Price display must be consistent across ALL LLM prompts.** Triage, VLM, and any future LLM-facing code must use the same three-way logic (known/free/unknown). See [[triage-vlm-context-inputs]].
- **Detail enrichment never overwrites existing good data.** Price, `posted_at`, and other fields are only filled if currently None.
- **Watchlist items bypass triage for a reason.** Triage killed 77% of valid watchlist matches. Don't re-add triage for watchlist items without measuring.
- **OG tags and JSON-LD are dead on Facebook.** Do NOT add OG/JSON-LD extraction. See [[facebook-og-jsonld-are-dead]].
- **`amount_with_offset*` fields are CENTS, not dollars.** Always divide by 100 before storing as price. See [[graphql-amount-units-cents-vs-dollars]].
- **Database upsert must include ALL enriched fields.** If a field isn't in the ON CONFLICT UPDATE SET, enriched values are silently discarded. Check `listing_repo.py` when adding new extractable fields.
- **Google has an undocumented Images-Per-Minute limit.** See [[google-vlm-ipm-undocumented-limit]].
- **VLM task cancellation ≠ provider failure.** The voting system's early-exit cancels the third provider. Don't increment failure counters on cancelled tasks — it demotes healthy providers.
- **Never append raw web search text to VLM reasoning.** Web search results are often garbage (wrong product matched by search engine). Track usage in `DealProvenance` instead.
- **Replacement-parts filter.** Watchlist search for "kitchen aid" matches $5 gaskets/valves. `_is_replacement_part()` catches these via part-number patterns + repair keywords. Only applied to watchlist listings where the interest isn't explicitly a "part."
- **Backlog listings go through the full filter chain.** Skipping filters on DB-recovered listings was the original bandaid bug. See [[unified-filter-pipeline]].

## Reference

- [[vlm-triage-pipeline]] — stage-by-stage architecture
- [[unified-filter-pipeline]] — filter chain + tag exemptions
