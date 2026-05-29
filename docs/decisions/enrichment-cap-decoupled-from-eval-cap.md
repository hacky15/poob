---
type: decision
status: active
date: 2026-05-29
tags: [scanner, enrichment, posted_at, freshness, coverage]
related: [[browse-path-ignored-configured-location]] [[multi-center-general-browse]] [[listing-freshness-verification]]
---

# Decouple the enrichment cap from the VLM eval cap

## Context

Post-fix measurement (2026-05-29, after the anon-browser self-heal restored enrichment) settled the `posted_at` question with hard numbers:

| listings since 16:08 | posted_at coverage |
|---|---|
| **enriched** (detail page visited) | **45/45 = 100%** |
| not enriched | 7/237 = 3% |

So `posted_at` **extraction is not broken** — detail-page enrichment yields a timestamp essentially every time it runs. The 81.6% NULL is purely a *reach* problem: only ~16% of listings get enriched. Anon GQL strips `creation_time` (FB, April 2026), so a listing that never reaches detail-page enrichment keeps a NULL timestamp and is hard-blocked from all notifications.

The limiter: the pre-enrichment cap used `deal_radar_max_evaluations` (50) — the same knob that caps VLM evaluation. On a big cycle (one cycle pulled 228), listings were truncated to 50 *before* enrichment, so most never got a timestamp.

## Decision

**Split the two caps.** Enrichment (cheap: a detail-page navigation) should run on a wider set than VLM evaluation (expensive: multi-provider vision voting).

- New `config.patrol_enrichment_cap: int = 75` — how many freshness-sorted listings to enrich per cycle.
- `deal_radar_max_evaluations` (50) stays the VLM cap.
- The pre-enrichment truncation now uses `_enrichment_cap`; `_evaluate()` independently re-caps to `_max_evaluations` for VLM. The extra enriched-but-not-VLM'd listings are still saved to the DB with their discovered timestamps — useful for backlog recovery and future cycles.
- `__init__` guards a non-int / below-eval-cap value (falls back to the eval cap), so a spec'd mock or misconfig can't shrink enrichment below evaluation.

Net effect: ~25 more listings per big cycle get a timestamp (→ become notification-eligible) without increasing VLM cost.

## Why 75

Bounded by the per-cycle safety budget: 75 detail-page navigations at ~5s each over 2 tabs ≈ 190s, plus collection (~90s) + triage/VLM (~60-120s), comfortably under the 600s cycle timeout. The per-listing 20s timeout and the 5-consecutive-miss early-bail cap the worst case. 75 is a meaningful lift (+50%) over the prior 50 without risking cycle-timeout abandonment on big batches.

## Honest limit

This raises *eligibility*, not necessarily *notifications*. The same measurement showed that even among timestamped listings, only 1 of 52 was <10 min old (42 were >6h) — FB's anonymous feed serves mostly hours-old inventory. So more timestamps mostly means more listings that then fail the 10-min freshness gate. The real freshness ceiling is being addressed separately via the operator's choice to pursue an authenticated browser (fresher feed); this enrichment-cap change is the complementary reach improvement and prep for when fresher inventory is available.

## Validation

- `TestEnrichmentCapDecoupled`: cap set ≥ eval cap; explicit cap (75) honored; behavioral test feeds 90 fresh listings and asserts enrichment receives 75 (not the eval cap 50).
- Full `test_patrol_engine.py` + `test_config.py` green.
- Post-deploy: `Pre-enrichment cap applied after=75 eval_cap=50` on big cycles; `post_enrichment.timestamp_coverage` should rise; watch the 600s cycle timeout isn't tripped on max-size batches.

## Follow-ups

- Authenticated-browser freshness path — operator-approved direction, but flagged against the standing "no scraping behind login walls / no ToS violations / no anti-bot bypass" rule; needs explicit go + scope before building. The repository already has an authenticated main-browser path (currently CDP-degraded); this would lean on / repair that rather than build net-new.
- Freshness-window arithmetic (10min vs feed age) still pending its own decision note.
