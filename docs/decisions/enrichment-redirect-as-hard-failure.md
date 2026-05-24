---
type: decision
status: active
date: 2026-05-24
tags: [scanner, enrichment, facebook, freshness, notifications]
related: [[husqvarna-enrichment-cross-contamination]] [[listing-freshness-verification]] [[just-listed-rework]]
---

# Enrichment redirects are hard failures, not silent fall-throughs

## Context

Facebook serves a marketplace recommendation feed at the detail URL when the original listing is sold, deleted, or made private. Pre-fix behavior in `detail_extractor.py`:

- Title-overlap check detects the mismatch (enriched title doesn't match original).
- Code blanks `detail.title/description/price`, KEEPS `detail.posted_at` and `detail.condition` from the wrong listing's page.
- Returns the original listing unchanged from `extract_listing_details`.
- `_enrich_one` sees `got_new_data=False`, leaves the listing in `to_evaluate` untouched.

Consequence: listing reaches VLM with no description, no timestamp (because GraphQL strips creation_time for anon, DOM badge was missed, and detail extraction was discarded). VLM evaluates blind, gets `evaluated=1`, never could fire a notification. Wasted VLM call per redirect; 4 of 19 listings per typical cycle (21%) hit this path.

## Decision

**Treat enrichment redirects as terminal listing-state failures, not transient extraction failures.**

A redirect means "Facebook says this listing is gone" — the original listing is unrecoverable for this poob deployment. Behavior:

1. **`EnrichmentRedirectedError`** raised from the discard branch of `extract_listing_details`. Signals a different class of failure than timeout or extraction error.
2. **`_enrich_one`** catches the error:
   - Marks the listing permanently `evaluated=1` in the DB so it does not return as backlog.
   - Sets `raw_data["_enrichment_redirected"]=True` on the in-memory listing.
   - Counts toward `redirected_count`, NOT `failed_count` (failures advance the consecutive-misses early-bail counter; redirects don't, because they're not a session/network problem).
3. **`_evaluate`** filters listings flagged `_enrichment_redirected` out of `to_evaluate` before SmartDealRadar runs. These never reach VLM.

## Alternatives considered

- **Keep silent fall-through, drop the contaminated `detail.posted_at`.** Simpler, but listings still reach VLM with empty description (degraded quality) and waste VLM calls on garbage signal.
- **Detect redirect by URL change before extracting.** Cheaper (skip the extraction work) but requires inspecting the post-navigation URL across browser-use's API surface. Could be a follow-up optimization if the redirect rate stays high enough to matter.
- **Soft-fail at the notify gate: when `posted_at is None`, evaluate but never notify.** Already the case today via `notify.blocked_no_timestamp` — but this is what produced the 0-notification symptom. The fix is to NOT spend VLM budget on listings the gate will reject.

## Why this is the right layer

Discard-detection lives in `detail_extractor.py` because that's where the title-overlap signal is available. Acting on it (skipping VLM, marking evaluated, flagging in-memory) lives in `patrol_engine.py` because that's where the eval pipeline runs. The exception type bridges the two layers without coupling them.

The vault rule "fix at the source, not the symptom" applies: the original symptom was "0 notifications." The surface fix is "lower notification threshold" or "widen freshness window." The actual source is enrichment silently letting unenrichable listings reach VLM. Fix at source.

## Consequences

- VLM credit savings: roughly 21% of detail-enrichment listings were redirects under the pre-fix flow; those VLM calls are now skipped.
- Less DB pollution: redirected listings no longer flow into evaluation-result tables with NULL description/timestamp.
- Backlog stays clean: redirects are evaluated=1 permanently, so they don't bounce back next cycle.
- `funnel.cycle` log line (added in same commit) makes this kind of attrition visible per-cycle: any future regression where listings die silently between stages will show up immediately as a count gap.

## Supersedes

Implicit prior decision: that contaminated detail extraction should preserve `posted_at` and `condition` from the wrong listing. That was wrong — those fields came from the recommendation page, not the original listing.
