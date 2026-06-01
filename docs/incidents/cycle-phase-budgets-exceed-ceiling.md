---
type: incident
status: resolved
date: 2026-06-01
tags: [scanner, patrol, cadence, enrichment, vlm, freshness]
related: [[enrichment-no-time-budget-cycle-abandonment]] [[main-browser-cdp-wedge-infinite-hang]] [[authenticated-discovery-sweep]]
---

# Per-phase time budgets sum past the 600s ceiling → heavy cycles abandon

## Symptom

Minutes after the authenticated feed was restored (2026-06-01), the first patrol cycle abandoned at 600s — and it was the *heaviest* cycle (the fresh feed delivered a backlog of 58 listings to enrich). Timeline:

```
21:08:06 Starting patrol cycle
21:10:30 General browse complete  auth_count=32 dom_count=18 gql_count=221 total=271   (~144s)
21:11:06 Enriching ... to_enrich=58
21:15:33 Detail enrichment complete  enriched=58 budget_exhausted=False               (~267s)
21:18:06 Patrol cycle hung past 600s — abandoned                                       (eval running ~153s, killed)
```

Enrichment finished *within* its budget; the VLM eval was ~25s from hitting *its* 180s timeout when the scheduler's 600s hard ceiling killed the whole cycle. An abandoned cycle loses all its deals **and** never reaches the end-of-cycle cookie-persist — and heavy cycles are exactly the high-value ones (most fresh inventory). Subsequent (lighter) cycles completed fine — 9 of 10 — but the structural overrun recurs whenever inventory is high.

## Root cause

The two phase budgets from [[enrichment-no-time-budget-cycle-abandonment]] were sized **independently**: enrichment `patrol_enrichment_max_seconds=300` and eval `patrol_evaluation_max_seconds=180`. With a ~180s sweep, the worst case is `180 + 300 + 180 = 660s > 600s` scheduler ceiling. Each phase respected its own budget, but their **sum** plus the sweep blew the cycle. The per-phase bounds prevent any single phase from running away; nothing bounded the *cycle as a whole*.

## Fix

Add a **shared cycle soft-deadline** `patrol_cycle_soft_budget_seconds=540` (set at cycle start in `run_patrol_cycle`), and clamp BOTH heavy phases to it:

- Enrichment ([patrol_engine.py](../../src/poob/scanner/patrol_engine.py) `_enrich_listings_from_detail_pages`): `deadline = min(now + enrichment_budget, cycle_deadline)`.
- VLM eval (`_evaluate`): `eval_timeout = min(eval_budget, cycle_deadline - now)`; if no cycle time remains (enrichment consumed it), **skip eval entirely** and defer the batch to the next cycle's backlog rather than starting an eval that can't finish.

This guarantees `sweep + enrichment + eval ≤ ~540s < 600s`, so a heavy cycle **completes-partial** (persists cookies, keeps the watchdog counter clean, emits whatever deals it scored) instead of abandoning. The freshest listings are enriched/evaluated first (freshness-sorted), so a partial cycle still surfaces the just-listed deals; the stale tail defers to backlog. Loosens no quality gate; the load-bearing 600s scheduler ceiling is unchanged.

## Validation

- `tests/unit/test_patrol_engine.py::TestCycleSoftBudget`: enrichment stops at the cycle deadline (not the larger phase budget); eval clamps to the remaining cycle time; eval is skipped when the deadline has passed; default budget present.
- Existing `TestEnrichmentTimeBudget` / `TestEvaluationTimeout` still green (cycle_deadline=None in those isolated calls → legacy behavior).
- Prod (post-deploy): heavy cycles should log `Skipping VLM eval — cycle soft budget spent` or complete-partial, and `Patrol cycle hung past 600s` should stop recurring on high-inventory cycles.

## Follow-ups

- The deeper lever for heavy cycles is the enrich-vs-eval cap mismatch (enrich 75 / VLM 50) and the GQL-stale flood feeding enrichment — see [[enrichment-no-time-budget-cycle-abandonment]] follow-ups. The soft-deadline bounds the symptom; gating the stale GQL contribution before enrichment would reduce the load itself.
