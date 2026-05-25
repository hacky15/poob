---
type: decision
status: active
date: 2026-05-25
tags: [scanner, freshness, triage, vlm, notifications]
related: [[just-listed-rework]] [[listing-freshness-verification]] [[husqvarna-enrichment-cross-contamination]] [[enrichment-redirect-as-hard-failure]]
supersedes: [[just-listed-rework]]
---

# Triage-freshness converges with notification-freshness

## Context

[[just-listed-rework]] (2026-04-22) deliberately decoupled two cutoffs:

- `listing_max_age_hours = 6` — evaluation/triage cutoff (what reaches VLM)
- `public_notification_max_age_minutes = 10` — public channel notification cutoff
- `watchlist_notification_max_age_minutes = 30` — watchlist DM cutoff

Rationale at the time: keep the eval pipeline wider than the notification cutoff so we could measure backlog, recover misses, and observe attrition.

Two facts changed since:

1. **Anonymous GraphQL stripped `creation_time`** ([[graphql_interceptor.py:26-30]] documents this; verified empirically). All timestamps now come from DOM badge parsing or detail-page enrichment.
2. **Audit on 2026-05-25** (DB-wide): 1,310 listings scraped past 24h, 1,075 evaluated, **only 1 listing was <10 min old at scrape time**. Of the 8,816 lifetime evaluations, 0 ever satisfied the notification gates. Then [[husqvarna-enrichment-cross-contamination]] surfaced that 80%+ of evaluated listings had no posted_at at all — VLM was scoring blind on items that could never fire a notification.

User standard, restated during the audit: *"we cant send out incredible deal noticiatoins to the public channel unless they are truly PERFECT. that's the requirement."*

The 6h triage cutoff served measurement, not the product requirement. Every VLM call on a 1h-old listing is wasted budget — the listing cannot satisfy a 10-min public gate or a 30-min watchlist gate.

## Decision

**Tighten triage cutoff, but not all the way to the watchlist gate.**

1. `listing_max_age_hours: int = 6 → 3` — VLM spends budget only on listings within 3h of posting. Tighter than the original 6h but with margin above the 30-min watchlist DM gate so that fresh-but-slowly-enriched listings still reach VLM. **Initial value was 1h; that proved too narrow** — FB's anon ranked feed serves mostly stale-popular listings, so 1h produced `vlm_evaluated=0` for 11 hours straight (verified in prod 2026-05-25). 3h is the smallest value that empirically allows VLM to keep producing scores.
2. **`FreshnessFilter` rejects `posted_at is None` at `POST_ENRICHMENT`** (skip at PRE_ENRICHMENT remains — the timestamp may still arrive during detail-page extraction). After enrichment, an unverified-age listing cannot fire a notification under any gate, so it must not reach VLM. **This part is load-bearing and stays.**

Combined effect: every listing reaching VLM has a verified timestamp AND is within a window where it might still hit a notification gate by the time evaluation completes. VLM credit is spent only on listings that can plausibly produce a user-visible outcome.

## Why supersede `just-listed-rework`

The original split-knob decision is still load-bearing for the cleanly-separated public/watchlist cutoffs. What changes:

- The justification for keeping the eval pool wide (backlog recovery, observability) no longer applies. The new `funnel.cycle` log line ([[husqvarna-enrichment-cross-contamination]]) replaces the observability need that the wide eval pool was serving.
- Backlog recovery via `get_unevaluated(max_age_hours=24)` still runs — backlog listings pass through the same FreshnessFilter and are correctly dropped if stale. The cap was never the right recovery lever; the filter chain is.

## Alternatives considered

- **Keep 6h, just fix no-posted_at rejection.** Saves the VLM-blind-eval problem but still spends VLM on listings >30min old that can never notify. Half-measure.
- **Lower to 1h (the original tightening).** Tested in prod — produced `vlm_evaluated=0` for 11 hours because FB rarely shows us listings <1h old in the anon ranked feed. Empirically too tight.
- **Lower to 0.5h (30 min, matching watchlist gate exactly).** Strictly worse than 1h for the same reason.
- **Configurable per-stage cutoff (different at pre vs post).** Adds knob complexity without product benefit — the notification gate is the binding constraint regardless.

The 3h value was settled after live prod data showed the structural problem: FB serves stale-popular listings to anon viewers, so the eval pool needs headroom above the user-facing notification gates to maintain a non-zero catch rate. 3h is the smallest value that keeps the pool non-empty without re-burning credit on truly-stale items the notify gate would reject anyway.

## Consequences

- Fewer VLM evaluations per cycle (estimated 60-80% reduction based on audit data — most listings >1h old never reach VLM now).
- Tighter convergence between triage and notification means clearer attribution: a deal that scores INCREDIBLE will fire unless blocked by per-user threshold or exclusion.
- `funnel.cycle` line's `enriched → vlm_evaluated` attrition will widen — that's correct, it reflects the new tighter gate.
- Backlog listings older than 1h are now permanently excluded from VLM. They were being excluded by the notification gate already; this just stops paying the VLM cost first.

## Validation

113 unit tests pass (filter + config). New tests:
- POST_ENRICHMENT rejects `posted_at is None`
- PRE_ENRICHMENT still skips `posted_at is None`
- POST_ENRICHMENT passes fresh listings
- POST_ENRICHMENT rejects over-age listings
- Disabled-filter behavior preserved across both stages

Post-deploy: watch `funnel.cycle` for the `enriched → vlm_evaluated` gap to widen and `ts_coverage_pct` to drop to 100% (no longer counting skipped no-timestamp listings as "enriched but not VLM'd" — they'll be rejected upstream by the post-enrichment filter).
