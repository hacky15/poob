---
type: incident
status: resolved
date: 2026-05-30
tags: [scanner, patrol, enrichment, cadence, freshness]
related: [[enrichment-cap-decoupled-from-eval-cap]] [[triage-freshness-converge-with-notify]] [[anon-browser-cdp-death-no-recovery]]
---

# Detail enrichment has no aggregate time budget → high-volume cycles abandoned at 600s

## Symptom

In a 12h audit (2026-05-30) two patrol cycles were abandoned at the scheduler's hard ceiling:

```
2026-05-30T13:27:07Z Patrol cycle hung past 600s — abandoned, scheduler continues to next interval
2026-05-30T13:42:37Z Patrol cycle hung past 600s — abandoned, scheduler continues to next interval
```

Both were the **only** two cycles in the window with `to_enrich=75` (every cycle with `to_enrich<=31` completed). Each was immediately preceded by `Enriching listings via detail pages ... to_enrich=75` with **no** subsequent `Detail enrichment complete` line. An abandoned cycle drops the *entire* cycle — enrichment, VLM evaluation, **and notifications** — so any genuinely-fresh deal discovered in those cycles was never delivered.

The trigger was the anon GQL rate-limit circuit breaker recovering at 13:18 and flooding the freshness-sorted candidate set to the `patrol_enrichment_cap=75` ceiling for the first time that day.

## Root cause

`PatrolEngine._enrich_listings_from_detail_pages` was bounded only **per-listing** (`_PER_LISTING_TIMEOUT_S=20.0`) and by a 5-consecutive-miss early-bail. It had **no aggregate wall-clock bound**. With `concurrency=2` and inter-batch stealth delays, 75 detail navigations take ~450–570s, which on top of ~110s of collection exceeds the 600s scheduler ceiling ([patrol_scheduler.py:201](../../src/poob/scanner/patrol_scheduler.py)). The per-listing timeout the prior code relied on does not help: 75 individually-bounded enrichments still **sum** past the cycle budget.

The 600s ceiling itself is deliberate and load-bearing (raised from 300s per the April-25 51h-hung-patrol incident); it must NOT be tightened. The defect was the unbounded phase underneath it.

A second, latent instance: the VLM stage `_evaluate` called `evaluate_batch` with no timeout either — a degraded/rate-limited cascade on a full survivor batch could blow the budget on a differently-shaped cycle.

## Fix

Bound the two cycle-dominating phases by **time**, not just pool size:

- **Enrichment** ([patrol_engine.py](../../src/poob/scanner/patrol_engine.py) `_enrich_listings_from_detail_pages`): compute a deadline at method entry (`time.monotonic() + patrol_enrichment_max_seconds`, default 300s) and break the batch loop when exceeded, logging `Detail enrichment time budget exhausted`. The candidate list is already freshness-sorted (`_freshness_key`, newest-first, timestampless last), so the listings dropped by the budget are exactly the stalest ones that can never pass a 10-min/30-min notification gate. A hard abandonment becomes graceful partial completion.
- **VLM evaluation** (`_evaluate`): wrap `evaluate_batch` in `asyncio.wait_for(timeout=patrol_evaluation_max_seconds)` (default 180s); on `TimeoutError` the cycle returns cleanly with no deals from that batch instead of running out the 600s ceiling.

Two new config fields (`patrol_enrichment_max_seconds`, `patrol_evaluation_max_seconds`); `0` disables each bound (legacy behavior). Both default comfortably inside 600s with headroom for sweep + notify.

This loosens no quality gate — it preserves the wide enrichment pool ([[enrichment-cap-decoupled-from-eval-cap]]) and the load-bearing POST_ENRICHMENT freshness filter ([[triage-freshness-converge-with-notify]]); it only caps wall-time on the freshest-first ordering.

## Validation

- `tests/unit/test_patrol_engine.py::TestEnrichmentTimeBudget` — budget disabled when unset; explicit value honored; loop stops after the first batch when the budget trips (controllable `time.monotonic`); budget=0 enriches all.
- `tests/unit/test_patrol_engine.py::TestEvaluationTimeout` — `evaluate_batch` hang returns gracefully with a `timed out` error and zero deals.
- Full `test_patrol_engine.py` + `test_config.py` green.
- Watch in prod: `Detail enrichment complete budget_exhausted=true` should appear only on high-volume (to_enrich→cap) cycles, and `Patrol cycle hung past 600s` should stop occurring on those cycles.

## Follow-ups

- The enrich-vs-eval cap mismatch (enrich 75, VLM 50) means ~25 enrichments per heavy cycle are discarded before VLM — intentional for timestamp recovery, but the time budget now caps the wasted tail. Re-examine whether the GQL-stale flood (multi-day-old listings with stripped `creation_time`) should be gated *before* enrichment rather than truncated during it — the deeper, scale-relevant lever.
- Optimal `patrol_enrichment_max_seconds` (300s default) is an inference; instrument how many *timestamped* listings the budget truncates vs only timestampless ones, and tune.
