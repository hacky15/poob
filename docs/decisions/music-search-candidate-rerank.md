---
type: decision
status: active
date: 2026-07-11
tags: [music, ytdl, search]
related: [[ytdl-search-best-guess-fallback]] [[music-player-architecture]] [[spotify-track-link-play-dead-end]]
---

# Music search: re-rank candidates by song-plausibility instead of take-first

## Context

[[ytdl-search-best-guess-fallback]] (2026-05-01) established best-guess-over-
dead-end and explicitly deferred further tuning "only after the data shows
it's needed." The data arrived on 2026-07-11:

- *"play copper and clay by Noah Sorely"* → queued **"Can Art Clay Copper be
  torch fired? Yep!" [30:59]** — a pottery how-to. Full token overlap
  (copper ✓ clay ✓), so overlap alone can't catch it; the 31-minute duration
  is the tell.
- *"play copper and clay by Nona Cryl"* → queued a **2-hour Chinese drama
  compilation** — zero overlap AND long-form.

Take-first-result had no notion of "is this plausibly a song?". Users read
these as "it misinterprets me" — the routing was fine; the search layer was
the failure.

## Decision

`AsyncYTDL.search` keeps its two-pass shape but adds **candidate re-ranking**:

1. **Pass 1** unchanged (`extract_info(query)` → ytsearch1). Returned
   immediately when plausible.
2. **Implausibility gate** (`_is_implausible_hit`): pass-1 hit longer than
   `LONGFORM_HIT_THRESHOLD_SEC` (900 s — matches `MAX_PREDOWNLOAD_DURATION_SEC`,
   the system's existing "not a normal song" line) for a query WITHOUT
   long-form intent → trigger pass 2 even though pass 1 found something.
   Direct URLs and unknown durations are exempt.
3. **Pass 2** (`ytsearch5`) now **re-ranks** all candidates (pass-1 hit
   included, with a +0.1 incumbent bonus) by `_relevance_score`:
   title-token overlap (stopword-filtered, 0..1) plus a duration-fit term —
   +0.3 for 1–15 min, −0.2 for 15–30 min, −0.5 beyond 30 min, livestream
   −0.25. Queries with long-form intent (`mix`, `playlist`, `hour`,
   `compilation`, `lofi`, …) skip the duration term entirely — a 2-hour
   throwback mix is exactly what was asked for (prod-observed and kept).

**The additive guarantee:** the pass-1 track always competes in the ranking
and pass-2-empty falls back to it — no query that returned a track before
returns `None` now. Best-guess-over-dead-end is preserved; the guess is just
better.

## Alternatives considered

- **Hard-reject long-form non-music results** ("couldn't find it" instead of
  the pottery video). Rejected: directly contradicts the best-guess decision,
  and "least-bad candidate plays" degrades gracefully when a song genuinely
  isn't on YouTube.
- **LLM-scored relevance.** An extra LLM call (latency + quota) in the hot
  play path for something a deterministic score handles; revisit only if the
  deterministic ranker demonstrably mis-picks.
- **yt-dlp `--match-filter` duration caps.** A hard filter, same
  contradiction as hard-reject, and it can't express "prefer, don't require."

## Consequences

- One extra `ytsearch5` round-trip (~1–3 s) ONLY when pass 1 looks
  implausible — rare; normal requests are unaffected (single call,
  short-circuit).
- `search_many` / Spotify import / autoplay tiers share the improvement
  wherever they call `search()` (autoplay's own candidate loops are
  untouched).
- Tunables live as class constants (`LONGFORM_HIT_THRESHOLD_SEC`,
  `_LONGFORM_QUERY_SIGNALS`, `_QUERY_STOPWORDS`, score weights) — adjust with
  evidence, per the original decision's discipline.
- Known trade-off: a legitimately long song (prog epics >30 min) with a
  short high-overlap alternative (e.g. a cover) may lose the ranking. Judged
  acceptable vs. the observed harm; the incumbent bonus softens it.

## Validation

`tests/unit/test_ytdl_search_rerank.py` (13 tests): both prod failures
reproduced and corrected; plausible-hit short-circuit (one network call);
long-form-intent queries untouched; empty-pass-1 re-rank; the additive
guarantee (widen-empty → pass-1 hit survives); URL exemptions; scoring and
gate boundary units.

Log signals: `ytdl.search widening to ytsearch5 reason=implausible_top_hit`
and `ytdl.search picked via rerank`.
