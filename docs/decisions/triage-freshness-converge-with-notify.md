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

**Kept the no-posted_at rejection. Backed off the cutoff tightening.**

The tightening attempts (1h, then 3h) both produced `vlm_evaluated=0` over multi-hour prod windows. The structural problem the user-facing audit revealed isn't fixable by tightening the eval cutoff — FB's anon ranked feed simply doesn't surface listings <3h old in any reliable volume, so any tight cutoff produces zero VLM activity. Settled state:

1. `listing_max_age_hours: int = 6` — back to the original. Eval pool needs to be wide enough to contain *something* or the notification path is structurally dead. The notification gates (10-min public, 30-min watchlist) enforce "just listed" at the product layer; the eval cutoff just keeps day-old garbage out.
2. **`FreshnessFilter` rejects `posted_at is None` at `POST_ENRICHMENT`.** This is the load-bearing correctness fix — listings reaching VLM without a verified timestamp can never satisfy any notification gate, so spending VLM credit on them is pure waste. Skip at PRE_ENRICHMENT remains (timestamp may still arrive during detail-page extraction).
3. **`patrol_moderate_interval_seconds: 600 → 300`** — sweep every 5 min during moderate hours instead of every 10 min. More chances to catch listings while they're still in their freshness window. Catches partially for the FB-feed-staleness issue without requiring an identity pool or paid endpoint.

Combined effect: VLM gets a non-empty pool to score, every scored listing has a verified timestamp, and we sweep often enough to occasionally catch a fresh listing. The user-facing notification gates do the rest of the work — they're strict by design and unaffected by the eval cutoff value.

## Why supersede `just-listed-rework`

The original split-knob decision is still load-bearing for the cleanly-separated public/watchlist cutoffs. What changes:

- The justification for keeping the eval pool wide (backlog recovery, observability) no longer applies. The new `funnel.cycle` log line ([[husqvarna-enrichment-cross-contamination]]) replaces the observability need that the wide eval pool was serving.
- Backlog recovery via `get_unevaluated(max_age_hours=24)` still runs — backlog listings pass through the same FreshnessFilter and are correctly dropped if stale. The cap was never the right recovery lever; the filter chain is.

## Alternatives considered (and the journey)

- **1h (first attempt, 2026-05-25 morning).** Reasoning at the time: triage should converge with the widest notification gate (30-min watchlist). Empirically produced `vlm_evaluated=0` for 11 hours straight. Too tight.
- **3h (second attempt, 2026-05-25 afternoon).** Reasoning: tighter than 6h, looser than 1h, give VLM SOME pool. Still produced `vlm_evaluated=0` for several hours. FB's anon feed simply doesn't surface that much fresh material to us.
- **6h (settled, 2026-05-25 afternoon).** Acceptance of the empirical reality: the eval pool needs headroom because FB controls what we see. The no-posted_at rejection at POST_ENRICHMENT prevents the no-timestamp leak that was the original problem; the wider cutoff lets enough listings through to keep VLM productive.
- **0.5h (matching watchlist gate exactly).** Strictly worse than 1h for the same reason; never tested.
- **Configurable per-stage cutoff (different at pre vs post).** Adds knob complexity without product benefit — the notification gate is the binding constraint regardless.

**Lesson learned:** the user-facing notification gates already enforce "just listed" — the eval cutoff is a budget control, not a freshness control. Tightening it cannot make notifications fresher; it only reduces the chance VLM ever sees a listing that could notify. The no-posted_at rejection is the meaningful correctness fix; the cutoff is just the floor.

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
