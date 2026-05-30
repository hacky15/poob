---
type: incident
status: resolved
date: 2026-05-30
tags: [scanner, filter, geo, enrichment, coverage]
related: [[multi-center-general-browse]] [[triage-freshness-converge-with-notify]]
---

# KnownFarLocationFilter cache poisoning — one borderline pin rejects a whole town's in-radius listings

## Symptom

In a 12h audit (2026-05-30) the PRE_ENRICHMENT `known_far_location` filter rejected **~270 listings** seeded by only **~9** `geo_distance` rejections. DB analysis of the 192 `"Madison, WI"` listings in the window: **104 in-radius (≤40mi), 9 out, 80 no-coords** — yet once the label was cached, *every* `"Madison, WI"` listing was rejected before enrichment (~91% false-positive on the with-coords pins). Five labels were affected the same way: Madison, Oregon, Theresa, Oconomowoc, Wales WI — each has more in-radius pins than far pins (Oconomowoc: 17 near, 1 far). Net effect: ~100 genuinely in-radius listings per 12h were killed before enrichment could even recover the coordinates that would clear them.

This starved **coverage/throughput** (it did not block deal output — 10 incredible deals still notified in the window), but on a long-uptime container the in-memory cache locked in within the first hour and over-rejected at a near-constant rate thereafter.

## Root cause

`KnownFarLocationFilter` keyed its learned cache on the **raw FB location label** ([listing_filter.py](../../src/poob/scanner/listing_filter.py)): `learn()` stored `listing.location.lower()` on any `geo_distance` rejection, and `__call__` rejected any listing whose label was in the set. A single FB town label spans the radius boundary — pins labeled `"Madison, WI"` range 0–44mi from center — so one borderline pin at 43.9mi (learned at the patrol_engine geo-reject site) poisoned the label for all of the town's in-radius listings. The reject fires at PRE_ENRICHMENT, before detail-page enrichment can recover the per-pin coordinates that the precise POST_ENRICHMENT `GeoDistanceFilter` would use to clear them.

The vault note [[multi-center-general-browse]] had asserted "KnownFarLocationFilter learned cache still works per-location-string" — that assumption never accounted for a single label straddling the boundary, and is corrected here.

The learn wiring compounded it: the engine learned only from `geo_distance` *rejections* (far pins). The in-radius *passes* (the corroborating "near" signal) were discarded because `FilterChain.filter_batch` returns kept listings as bare `Listing`s without their verdicts — so the filter could never see that a town also had in-radius pins.

## Fix

Track a **near-veto** alongside the far-set, sourced from real in-radius observations:

- `KnownFarLocationFilter` now holds `_known_far` **and** `_known_near`. `learn_far(loc)` caches a far label *only if it has never been observed in-radius*; `learn_near(loc)` records a confirmed in-radius pin, vetoes the label from the far-cache permanently, and un-poisons it if already cached. `__call__` rejects iff `loc in _known_far and loc not in _known_near`.
- The engine learn site ([patrol_engine.py](../../src/poob/scanner/patrol_engine.py)) now feeds **both** signals: for each kept listing it re-runs the held `self._geo_filter` and, on a confirmed in-radius verdict (OK, not `skipped:missing_data`), calls `learn_near`; for each `geo_distance` rejection it calls `learn_far`. Near is learned first so it wins within a cycle. Reusing the held `GeoDistanceFilter` instance keeps the near/far decision in one place — no haversine duplication.

Because a boundary-straddling town's in-radius pins are observed on its very first (un-cached) cycle, the label is vetoed before it can ever be cached. A genuinely-far town (every pin far, e.g. Plymouth WI) has no near observation, so it is still correctly cached and skipped — the optimization's intent is preserved.

This **loosens no quality gate**: the precise POST_ENRICHMENT `GeoDistanceFilter` still does the per-pin check. It is not a deliberate-soft-fail flip — it relaxes an over-aggressive PRE reject back toward the safe skip-to-enrichment behavior.

## Validation

- `tests/unit/test_listing_filter.py::TestKnownFarLocationFilter` (previously **zero** coverage on this filter): learn-then-reject a genuinely-far town; the boundary-straddle regression (a near-observed town must not be rejected even if a later far pin tries to learn it); near un-poisons an already-cached label; empty-location skip; case/whitespace-insensitive.
- Full `test_listing_filter.py` (95) and `test_patrol_engine.py` green.
- Watch in prod: `known_far_location` rejection volume should drop sharply and track actual genuinely-far towns; in-radius `"Madison, WI"`/`"Oregon, WI"` listings should reach enrichment.

## Follow-ups

- Optional refinement: key the far-set on rounded coordinates and graduate by proximity (a 43.9mi pin is different from a 200mi pin). Not needed to fix the documented poisoning; the near-veto is sufficient.
- The residual edge — a town whose *first-ever* sighting is exclusively far pins, then later has in-radius pins — is un-poisoned via watchlist-exempt or backlog paths that bypass the PRE cache; acceptable and rare.
