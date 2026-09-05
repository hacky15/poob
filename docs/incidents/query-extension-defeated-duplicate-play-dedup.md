---
type: incident
status: resolved
date: 2026-08-31
tags: [brain, music, voice, dedup, router]
related: [[music-tool-call-robustness]] [[voice-llm-model-deprecated-and-never-wired]] [[speculative-music-wrap]]
---

# Router-truncation guard extended the query AFTER dedup keyed on it — same song queued twice

## Symptom

Live production report, first request of a fresh voice session, both songs
audibly queued back to back:

```
01:29:22  "Hey, Poob. Play crank that by pickle."
          -> routed query='crank that by pickle' (no extension fired --
             the raw message carried no extra trailing words)
          -> Queued "Pickle - Crank That" (a literal-token-match junk hit)

01:29:38  "Hey Poob play crank that by pickle Oh, yeah, dude."  (+16s,
          inside the 20s dedup window; same user reacting to the wrong
          song and re-asking, nearly verbatim)
          -> LLM routed query='crank that by pickle'  <- IDENTICAL to the
             first request's routed query
          -> _prefer_full_play_span extended it to 'crank that by pickle
             Oh, yeah, dude' (a legitimate extension -- the raw message
             really does carry that trailing span)
          -> Queued "Soulja Boy Tell'em - Crank That" (the correct song,
             on top of the first, unsuppressed)
```

`_is_duplicate_play` exists for exactly this shape of retry
([[music-tool-call-robustness]], decision C) and did not fire.

## Root cause

`_is_duplicate_play(guild_id, user_id, query)` ran on `query` **after**
`_prefer_full_play_span` (the router-truncation guard added 2026-07-16 for
the "play home or let the barts out" → query='home' bug) had already
reassigned it. The two mechanisms were built independently, months apart,
and never checked for interaction.

The LLM router produced the *same* routed query both times
(`'crank that by pickle'`) — a clean, byte-identical retry at the routing
level. But the extension guard only fires when the routed query is a
strict substring of the raw message's trailing span, and only the second
message's raw text happened to carry the extra `"Oh, yeah, dude"` tail. So
one request's dedup key stayed un-extended and the other's got extended,
and the two never matched:

- recorded key: `'crank that by pickle'`
- second lookup key: `'crank that by pickle Oh, yeah, dude'`

This is a different shape than the STT-garble case in
`docs/research/voice-session-audit-2026-07-29.md` ("Diddy Heilett" vs
"Diddy Heil Epstein" — genuinely different strings even before any
extension, correctly left unfixed). Here the *routed* query was
byte-identical both times; only a downstream, unrelated correction
(extension) broke the match.

## Fix

Dedup now keys on the query **as originally routed** (post-scrub,
pre-extension) — captured into a new local, `dedup_query`, immediately
before the extension logic runs. The extended/corrected query is still
used, unchanged, for the actual search — extension itself is not touched
or weakened.

Two call sites, both with the identical extension-before-dedup ordering,
both fixed the same way:

- `_handle_music` (`src/poob/brain/poob.py`)
- `_handle_music_voice_streaming` (speculative-wrap voice path)

Each site also had a second, related read of the query for
`_clear_play_on_failure` (`play_query_for_dedup`) that was still reading
the post-extension value from `tool_args`. Left alone, this would have
silently desynced from the new `dedup_query`-keyed record: a failed play
would fail to clear its dedup entry, blocking a legitimate retry for the
rest of the 20s window. Both sites' `play_query_for_dedup` now derive from
`dedup_query` as well, guarded by the same `action == "play"` check
`dedup_query` is itself scoped to (it's only ever assigned inside that
branch, so the guard also prevents a `NameError` on non-play actions).

## Validation

- New test file `tests/unit/test_music_dedup_survives_query_extension.py`,
  three tests, reproducing the exact production sequence (same transcripts,
  same 16s gap) against both call sites, plus a narrow direct unit test of
  `_is_duplicate_play` itself.
- **Mutation-verified**: reverted both `_is_duplicate_play` call sites back
  to keying on `query` (the bug), confirmed the two behavioral tests fail
  with the exact reproduced double-queue (`music handler invoked 2x with
  ['crank that by pickle', 'crank that by pickle Oh, yeah, dude']`),
  restored the fix, confirmed all three pass again.
- Full unit suite: 1968 passed, 1 skipped, no regressions.
- `ruff check` / `ruff format` clean on both touched files.

## Follow-ups

- The hallucination-correction branch (`sn_query` re-derivation,
  `music.play re-extracted from raw after hallucination drop`) also
  reassigns `query` after `dedup_query` is captured, and is *not* covered
  by this fix — `dedup_query` stays keyed on the pre-hallucination-fix
  value in that branch too. Not changed here: no production evidence of
  this combination (stale-context hallucination + rapid retry
  co-occurring) has been observed, and speculatively widening the fix
  without evidence risks its own regression. Revisit if a log ever shows
  it.
- Same general shape as [[voice-llm-model-deprecated-and-never-wired]]'s
  wiring gap: two individually-correct mechanisms, added independently,
  whose composition was never exercised until a specific production
  message pair hit it. No structural safeguard currently catches "two
  functions mutate the same variable in sequence, and a check downstream
  assumes it still means what it meant upstream" — this class of bug will
  recur elsewhere in this file unless call sites are audited for it
  directly.
