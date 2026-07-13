---
type: incident
status: resolved
date: 2026-07-03
tags: [music, brain, text, wrap, persona]
related: [[music-text-replies-are-toob]] [[music-player-architecture]]
superseded_by: [[music-text-replies-are-toob]]
---

# Text music replies narrated absent deal categories ("no items, no prices…")

> **Backfilled 2026-07-12.** This note was referenced by code comments and
> `test_music_text_wrap.py` since the fix shipped (commit `c0647a0`) but was
> never actually written — recovered here from the test-file narrative so the
> wrap lineage is traceable. The Poob text wrap this incident introduced has
> since been REPLACED by the shared Toob wrap — see
> [[music-text-replies-are-toob]]. The anti-deal-vocabulary contract below
> survives in that wrap's tests.

## Symptom

Prod, 2026-07-03: "@Poob play gobble glitch remix 808 backwoods" (plain text
play request) was answered with *"Sure thing—no items, no prices, no
questions, no confirmations. Just the song's name, length, and the fact it's
playing."* — a bizarre, off-topic, oversized reply to a one-line status.

## Root cause

The text-channel short-response path in `_handle_music` reused
`_wrap_in_personality`, which is built for DEAL responses: its assistant-turn
label ("[My deal system says: …]") and instruction ("include items, prices,
questions asked, confirmations") are deal-shaped. A music result has none of
those categories, so the model narrated their ABSENCE instead of relaying
the song.

## Fix (as shipped then; since superseded)

A dedicated `_wrap_music_response_text` with a correctly-labeled
("[Music system result: …]"), music-only instruction and a tight token cap
(≤40, matching the Toob voice wrap). Same-day adversarial re-review also
pinned text to the neutral persona level (5) instead of the guild's rolled
voice horniness.

**2026-07-12:** that function was deleted when text music replies moved to
the Toob wrap (operator decision). The regression guards — "Music system
result" label, no deal vocabulary in the instruction, ≤40-token cap, raw
fallback — now run against the shared `_wrap_music_response`.

## Validation

`tests/unit/test_music_text_wrap.py` — the guards carried across both
generations of the fix; deal wrap (`_wrap_in_personality`) proven untouched
both times.
