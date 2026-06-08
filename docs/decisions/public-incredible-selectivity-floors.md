---
type: decision
status: active
date: 2026-06-07
tags: [scanner, deal-radar, selectivity, vlm, notifications, public-feed]
supersedes: []
related: [[watchlist-honors-threshold-not-freshness]] [[triage-freshness-converge-with-notify]] [[vlm-cascade-dead-rung-cleanup]]
---

# Public "INCREDIBLE" selectivity floors (de-clutter the public feed)

## Context

An audit of the last 100 public deals found **all 100 scored INCREDIBLE and all
100 were sent** — far too much clutter. Three failure classes:

1. **VLM market-value hallucination.** `Toaster $20 → "$150"`, `Free bricks →
   "$250"`, `Scentsy Pods $5 → "$90"`. The prompt said "fair market value +
   depreciation", which the VLM read as retail-minus-a-little.
2. **Cheap-commodity high-%.** `Pampered Chef waffle pan $7→$18` ($11 saved),
   `storage containers $4→$15`, `shot glasses $1→$3`. The `min(flat,
   proportional)` floor for items <$50 let a 100%-off cheap item qualify on a
   trivial absolute saving.
3. **Non-items.** `Rummage Sale! Last Day!`, `Garage Sale 2307 …`, `FREE AT
   CURB` — sale events, not products. `prompts.py` even rewarded them as urgency.

The operator: *"we need more determinism … more restrictive … likely a prompt
thing too … be absolutely surgical to not enforce any edge case mishandling."*

Hard constraint: this is the **PUBLIC feed only**. The watchlist DM path
([[watchlist-honors-threshold-not-freshness]]) is gated SOLELY by the per-item
`notification_threshold` and must NOT regain a deal-quality gate.

## Decision

Seven coordinated, surgical changes. The crucial architectural call: the
sale-event reject goes in **`GarbageFilter`** (a structural, watchlist-safe
gate), NOT in `_detect_misleading_listing` (which `return None`-hard-blocks
*before* the watchlist branch and would silently kill watchlist DMs). Every
value/dollar/worth-attention gate is wrapped `watchlist_context is None`.

1. **Non-item gate** — `GarbageFilter` rejects rummage/garage/estate/yard/barn/
   tag sale, curb-alert, "everything must go" via a word-boundary regex.
   Ambiguous `moving sale` / `multi-family` / `neighborhood sale` are
   **deliberately excluded** (they attach to legit single-item titles and are
   the only path that could suppress a watchlist DM).
2. **VLM resale framing** — Step 4 now asks for the *typical used local
   same-day resale price* (not retail/MSRP), "when unsure estimate LOW",
   commodity ≤~4× asking, consumables ≈ $0, branded/appliance/tool/furniture
   exempt. The load-bearing fix for value hallucination.
3. **`worth_attention` field** — new `VLMEvaluation.worth_attention` (fail-OPEN:
   default + omitted-parse = `True`, so weak cascade rungs can't black out the
   feed). On the public path a hard `False` caps the score at FAIR. Plus the
   incredible rubric tightened and sale events dropped from `urgency_signals`.
4. **Absolute $-floor** (`_ABS_DOLLAR_FLOOR`, INCREDIBLE = `$50`, public-only):
   the proportional <$50 path can never go below it → trivial-saving cheap junk
   can't be INCREDIBLE. Threaded as `is_public` so the watchlist path is unaffected.
5. **Value-multiple cap** (`deal_max_value_multiple` = `4×`, public-only):
   clamps an implausible unbranded value; a *single* retail/MSRP comp does NOT
   defeat it (that's the toaster case). Branded items, high-value categories,
   and multi-sample non-retail comps are exempt.
6. **Free-item value + identifiability floor** (public-only, downgrade-only,
   watchlist early-exit): a free item must clear `free_item_min_value` ($40) and
   be an identifiable resaleable product (`_HIGH_VALUE_UNBRANDED_KEYWORDS` /
   brand) to keep top tiers; non-resaleable bulk/consumables (`bricks`,
   `candle supplies`, `mens clothes`) → FAIR; below `free_item_incredible_min_value`
   ($80) or unidentifiable → cap GREAT (graceful degrade, never killed).
7. **Config plumbing** — all four thresholds in `AppConfig`, threaded with
   signature defaults so existing call sites can't break.

All thresholds are env-tunable; ship these defaults and tune against the feed.

## Residual hole (documented, NOT closed)

The numeric value-multiple cap is bypassed when the VLM **both** inflates value
**and** claims a brand at `confidence >= 0.7` (orchestrator.py unbranded-cap
bypass), and for `_KNOWN_BRANDS` consumables (Scentsy, Pampered Chef). For those,
the **prompt** (Change 2/3: consumables ≈ $0 resale, worth_attention=false) is
the only lever — there is no programmatic catch. This is a known residual, not
full closure. Also noted: `_listing_has_no_brand` uses substring matching, so
"**Ge**neric", "t**oaster**"→"oster" register as branded and skip the cap — a
pre-existing quirk, left as-is to avoid wide blast radius.

## Edge cases preserved (test-verified)

- Watchlist matches exempt from all gates (worth_attention, abs-floor,
  value-cap, free-floor); a cheap/free/garage-sale-titled watch match still DMs.
- Legit priced INCREDIBLE survive: Dell XPS, Roomba, Samsung TV, Clavinova,
  snowblower, Whirlpool dryer, Craftsman saw ($10→$150).
- Legit free survive: treadmill, washer, Nugget ice machine (added to the
  high-value keyword set as a regression guard).
- Boundary: free `value_high == 80` stays INCREDIBLE-eligible.

## Validation

34 + 5 + 25 new unit tests (GarbageFilter sale events, worth_attention fail-open
parse, full clutter/legit/watchlist selectivity matrix). Full suite green.
Post-deploy: pull the next ~50 public deals and confirm the cheap-commodity /
hallucinated-value / non-item classes are gone while branded/appliance deals
still flow.
