---
type: incident
status: resolved
date: 2026-07-21
tags: [brain, music, routing, safety-net, false-positive]
related: [[music-routing-prompt-thoughtfulness]] [[groq-429-fallback-routed-to-deals]] [[control-command-misroute-by-weak-rung]] [[music-failure-census-2026-07-17]]
---

# "Do you think we should play hard or standard?" — a normal question played a random video

## Symptom

Operator, live, furious: *"literally just played a song when I CLEARLY
CLEARLY CLEARLY ASKED IT A NORMAL QUESTION."* Production `voice.log`,
2026-07-21 01:45:31:

```
01:45:31  Dual: wake word addressed  text="Hey, Poob. Do you think we should play hard or standard?"
01:45:32  Safety net caught missed music intent  query="hard or standard"
01:45:34  music.response  "Queued Dark Path HARD STANDARD Guide | No Monkey Knowledge - BTD6 [4:23]"
```

The user asked Poob's **opinion** on a BTD6 (Bloons Tower Defense 6)
difficulty-mode choice — a normal conversational question — and it queued a
random gaming guide video instead of answering.

## Root cause

**The LLM routing cascade got this right.** There is no `poob.tool_route`
line anywhere near this timestamp — Groq, Gemini, NVIDIA, and Scout all
independently declined to call `music_assistant` for this message,
correctly reading it as conversation, not a command (validating the June 2
prompt fix in [[music-routing-prompt-thoughtfulness]]).

The deterministic **`_music_safety_net` play-intent backfill** then
**reversed that correct decision**. Its `play_signals` gate matches on
`f" {s}" in lower` — a bare substring check with no grammatical awareness —
so any message containing `" play "` anywhere triggers the backfill,
regardless of whether "play" is a command verb or, as here, embedded in a
subordinate clause of an opinion question ("do you **think** we should
**play** hard or standard"). The extractor then took everything after
`"play "` — `"hard or standard"` — and shipped it to YouTube search, which
confidently resolved a plausible-sounding but wrong video.

[[music-routing-prompt-thoughtfulness]] explicitly named this exact risk
class when the safety net was kept as an "un-invested-in backstop" on
2026-06-02: *"the existing brittle safety net **would** fire on 'let's play
a game'."* That note's own exit plan said: *"if post-deploy telemetry shows
the LLM now routes reliably, retiring/tightening it is a clean follow-up."*
The LLM does route reliably (it did here); this is that follow-up, with
production evidence in hand.

## Fix — v1, and why it was replaced same-day before shipping

**v1** added a guard checked before both play-related overrides: a single
regex (`_NOT_MUSIC_PLAY_RE`) enumerating whole VERB PHRASES as non-music
signals — `"do you think"`, `"should we/i/you play"`, `"let's/wanna/want to
play"`, plus idioms — matched anywhere in the message via unanchored
`.search()`.

Before shipping, an adversarial multi-agent review (four independent
lenses, each finding double-verified by two more agents) proved v1 unsound
on three fronts, all CONFIRMED against the real code:

1. **False positives** — "I want to play some jazz", "let's play some
   tunes", "wanna play some Beyonce" matched the same verb-phrase patterns
   as "let's play a game" and got silently suppressed. Voice assistants are
   routinely addressed with "I want to play X" phrasing; matching on the
   verb alone can never distinguish that from "I want to play a game".
2. **False negatives** — "What do you **reckon**, play attack or defense?"
   and "Should we **really** play hard or standard?" (one interposed word)
   both defeated the rigid phrase enumeration and reproduced the exact
   incident. Natural-language paraphrase space is not enumerable.
3. **Unanchored whole-message matching** — a genuine LEADING "Play Betty
   Davis eyes..." command (the exact stale-echo shape the misrouted-play
   override exists to fix) got suppressed by an unrelated TRAILING "wanna
   play something after" clause in the same compound, STT-crosstalk-laden
   message — architecturally the most serious finding, since it broke a
   *different*, already-working fix.

## Fix — v2 (superseded before shipping — see v3)

Rebuilt around the actual distinguishing signals, each scoped to where it's
actually meaningful rather than the whole message:

- **Game-reference carve-out moved from VERB to SPAN CONTENT.** Instead of
  matching "let's play"/"wanna play"/"want to play" as phrases, the
  extracted span (the same extraction the overrides already use) is checked
  after extraction: if every real content word in it is a generic
  game-reference noun (`game`/`games`/`round`/`match`), it's not music —
  regardless of which verb introduced it. Fixes the false positives (real
  content like "jazz"/"beyonce" is never in that set) while still catching
  "let's play a game" (content = `{game}`). **Deliberate trade-off:** a
  specific game TITLE ("wanna play Among Us") is no longer caught by the
  code-level backstop — enumerating titles risks the same false-positive
  class (a real "Minecraft"/"Fortnite" parody-song request exists). Left to
  the LLM/prompt layer, which the incident's own log evidence proves
  handles it correctly in practice.
- **Opinion-question detection narrowed to near-zero-collision phrases**
  ("do you think", "what do you think", "what do you reckon", "what's your
  take", "your take/thoughts on") — dropped bare "should X play" and bare
  "you think" entirely, since both were proven to collide with real
  requests. The residual gap (a differently-phrased opinion question
  without one of these anchors) is an **accepted, documented,
  evidence-first-deferred limitation** — not silently ignored, and
  consistent with the codebase's established discipline elsewhere (control
  override phrase sets, junk-title markers: extend only with evidence).
- **Scoped, not whole-message.** The opinion-question check runs only
  against the LEAD-IN text strictly *before* the matched play-verb
  occurrence (`_find_play_verb_match`, a helper shared with
  `_extract_play_query_span` so both agree on exactly where the verb sits).
  Idioms whose content follows "play" ("play it cool"/"play it safe") are
  checked against the extracted SPAN itself. Only short, low-collision-risk
  praise idioms ("good play", "playing with me", "stop playing") remain
  unconditional whole-message checks — a small, explicitly accepted
  residual risk, not the broad one v1 had.

The prompt layer (`_MUSIC_ROUTING_RULES`) was **not** touched in any
version — the slim routing prompt was already at its length-budget ceiling,
and the router already classifies these messages correctly without
reinforcement; the bug was entirely in code reversing a correct decision.

## Fix — v3 (shipped)

Before shipping v2, a SECOND independent adversarial review (four fresh
lenses, each finding double-verified by two more agents, run specifically
*because* the first review had already found v1's confident-looking design
unsound) found that v2 had made the same class of mistake twice more, in
narrower forms — every scoped check it added was scoped to the wrong
boundary:

1. **Lead-in scoped to the whole pre-verb prefix, not the local clause.**
   "What's your take on the new Kanye album, **play** Flashing Lights" —
   the album commentary is a *different, unrelated* clause from the play
   command, but `_NOT_MUSIC_LEADIN_RE` searched the entire text before the
   verb and matched "your take" anyway, suppressing a real trailing
   request. Mirror image of v1 finding #3, now on the leading side.
2. **`_NOT_MUSIC_IDIOM_RE` searched the whole message unconditionally.**
   "Stop playing this, **play** some jazz instead" — an ordinary
   control-then-request compound utterance — tripped `\bstop playing\b`
   before the verb-scoped checks even ran, silently dropping the real
   trailing "play some jazz" when no tool was routed.
3. **The game-reference span check required an exact token-set match.**
   "let's play **a fun** game" / "wanna play **one more** game" — any
   ordinary adjective or quantifier attached to the noun defeated the
   `content <= _GAME_REFERENCE_WORDS` subset check, letting extremely
   common non-music phrasings fall through as if they were song requests.
4. **`_find_play_verb_match` selected by prefix-list priority, not
   position.** For "**Play** Bohemian Rhapsody, let's **play some**
   games.", the loop tried `"play some "` (earlier in the priority tuple)
   before bare `"play "`, found it in the *trailing* clause, and returned
   that match — discarding the real leading song title entirely and
   extracting `"games"` as the query. Same failure for `"put on some jazz.
   wanna **play** something after"`. This is finding #3's failure class
   again, reintroduced through the verb-matcher itself rather than a
   phrase list.
5. **`_GAME_REFERENCE_WORDS` included "round"/"match"**, both real
   song-title words ("Round and Round" — Ratt 1984 / Selena Gomez ft. Flo
   Rida) that the exact-subset check then misclassified as a game
   reference once stopwords stripped the span down to just that token.
6. **The "it cool"/"it safe" idiom prefix was a rigid literal match** —
   "play it **real** cool" (one interposed word, the same paraphrase shape
   that broke v1's opinion-phrase enumeration) defeated it.

**v3's fix is one architectural change, not six patches**: every check is
now bounded to the **clause containing the matched play verb**
(`_clause_bounds` — nearest sentence/comma boundary on each side), instead
of the whole message or an arbitrary prefix, and `_find_play_verb_match`
now returns the **leftmost** verb occurrence in the message (tie-broken by
longest/most-specific prefix), not the first match by list priority. One
shared primitive fixes findings #1, #2, and #4 at the same time, because
all three were the same root mistake (matching outside the clause that
actually matters) wearing different masks. Findings #3, #5, and #6 are
narrower, independent fixes: a small closed-set of pre-noun modifiers
(`_GAME_REFERENCE_MODIFIERS`) stripped before the game-word subset check;
`"round"`/`"match"` dropped from `_GAME_REFERENCE_WORDS`; the idiom-prefix
regex now tolerates one interposed word, mirroring the tolerance already
built for the opinion lead-in.

**New deliberate trade-off, consistent with the existing evidence-first
discipline:** clause-scoping the lead-in check means a genuinely
comma-separated opinion question — anchor phrase and play verb in
*different* clauses ("What do you reckon, play attack or defense?") — is
no longer caught. The real production incident had **no** comma at all
("Do you think we should play hard or standard?"), so the primary case
stays fixed; the narrower comma-separated variant falls through to the LLM
router, which the incident's own log evidence shows already handles
thoughtful questions correctly in the large majority of cases. This is the
same asymmetry that justified every other narrowing in this incident: this
guard is a backstop, not the primary classifier — a false negative here
means "no backstop, the primary path usually still works"; a false
positive means "actively defeat an already-necessary recovery attempt."

## Validation

- Second adversarial review (v2 as the target): 4 dimensions, 8 raw
  findings, all 8 survived independent double-verification (0 refuted) —
  6 confirmed as real, distinct defects after removing near-duplicates;
  every one fixed in v3 and individually pinned in
  `test_documented_v2_bug_reports_are_fixed`.
- `tests/unit/test_routing_rules.py`: the exact production string
  reproduced end-to-end (→ `(None, None)`, unchanged); every CONFIRMED
  finding from BOTH reviews individually pinned
  (`test_documented_v1_bug_reports_are_fixed`,
  `test_documented_v2_bug_reports_are_fixed`); both architectural
  compound-message regressions pinned separately, leading and trailing
  (`test_leading_play_command_survives_a_trailing_unrelated_clause`,
  `test_unrelated_leading_clause_does_not_suppress_a_real_trailing_command`);
  the idiom-in-unrelated-clause and cross-prefix-priority regressions
  pinned end-to-end through `_music_safety_net`
  (`test_idiom_in_unrelated_clause_survives_end_to_end`,
  `test_unrelated_leading_clause_survives_end_to_end`); all accepted
  trade-offs pinned explicitly so a future change is deliberate
  (`test_specific_game_titles_are_a_documented_accepted_gap`,
  `test_comma_separated_opinion_anchor_is_a_documented_accepted_gap`,
  `test_round_and_match_game_words_are_a_documented_accepted_gap`); every
  vault-documented real catch and every v1/v2 false-positive re-verified as
  working; control-override precedence unchanged.
- v3 independently re-verified against 47 traced cases (original incident,
  every confirmed finding from both reviews, every documented trade-off,
  every pre-existing vault catch) via direct code execution — 0 failures —
  before the test suite was updated, then again via the test suite itself.
- Full unit suite: 93/93 in `test_routing_rules.py`; 1914/1921 in the full
  `tests/unit/` sweep (the 7 non-passing tests are pre-existing environment
  issues unrelated to this change — 4 are `asyncio.get_event_loop()`
  behavior differences between this machine's Python 3.13 and the project's
  3.11 target, and a `MUSIC_TOOL` token-budget test drifting by 2 tokens
  from an apparent tokenizer version difference; neither touches any file
  this fix modifies). Ruff/mypy clean on all new code.
- Post-deploy: `Safety net caught missed music intent` should never again
  fire for a question-form or game-reference message that isn't a
  documented residual gap; watch for recurrence of the residual gaps below
  as the evidence trigger for extending the relevant pattern further.

## Follow-ups

- **Documented residual gap — RESOLVED 2026-09-11, see addendum below.**
  Opinion questions phrased WITHOUT one of the recognized lead-in anchors
  (e.g. bare "should we play X or Y" with no "do you think"/"reckon"
  framing) can still slip through, by design — the review proved that
  catching these via phrase enumeration reliably breaks real requests
  instead. Extend `_NOT_MUSIC_LEADIN_RE` only with new production
  evidence, not preemptively.
- **Documented residual gap (new in v3):** a comma-separated opinion
  question — anchor phrase and play verb in different clauses ("What do
  you reckon, play attack or defense?") — is no longer caught, since the
  lead-in check is now scoped to the clause containing the verb. The real
  incident had no comma; extend only with new production evidence of this
  specific shape recurring.
- **Documented residual gap:** specific game titles ("Among Us",
  "Minecraft") are not recognized as non-music by the code-level backstop —
  relies on the LLM/prompt layer.
- **Documented residual gap (new in v3):** "round"/"match" are no longer
  recognized as game-reference words (dropped due to real song-title
  collisions) — "let's play a round"/"one more match" fall through to the
  LLM/prompt layer, same reasoning as specific game titles.
- **Pre-existing, separate limitation, re-surfaced (not fixed) during both
  reviews:** `_extract_play_query_span`'s crosstalk trim
  (`_trim_trailing_crosstalk`) only cuts at sentence terminators (`.`/`!`/
  `?`), not commas — a compound message joined by a comma ("Play Bohemian
  Rhapsody, let's play some games.") extracts an untrimmed, crosstalk-laden
  query ("Bohemian Rhapsody, let's play some games") rather than a clean
  one. The v3 leftmost-match fix guarantees the real content is no longer
  *discarded* (the pre-v3 failure mode), only that it isn't perfectly
  trimmed. Deliberately left unchanged: extending crosstalk-trimming to
  commas risks truncating real comma-containing song titles typed via text
  ("Hello, Goodbye"). Out of scope here, tracked as a follow-up.

## Addendum (2026-09-11) — the documented residual gap recurred, now fixed with real evidence

Live production, operator report: *"why did it try to play music when i
said 'what map should we play'? i really really hope this stupid system
doesn't actually think i want to play music whenever i say the word
'play'."* Log confirms the exact documented residual gap from the
Follow-ups section above, not a broader failure:

```
02:50:10  Dual: wake word addressed  text="Hey, Poob. What Rainbow Six Siege map should we play on?"
          → router correctly returned NO TOOL
02:50:10  Safety net play span carries no content — deferring to 'Play what?'  span=on
02:50:10  Safety net caught missed music intent  query=
02:50:11  First sentence ready  sentence='play what?'  voice=toob
```

No recognized opinion anchor ("do you think"/"reckon"/"your take") appears
before "play", so `_NOT_MUSIC_LEADIN_RE` didn't fire; the router got it
right and the code-level safety net reversed the correct decision — the
same failure *class* as the original incident, this session's new
production evidence for the accepted gap.

### Fix

Rather than enumerating more opinion phrases (the exact approach v1/v2
already proved unsound), added a narrower, more general signal:
`_WH_QUESTION_LEADIN_RE` (`what`/`which`) checked ONLY when the extracted
play-span has NO surviving content after stopword filtering. Rationale: a
real play command always names something ("play some jazz" — content
survives); "play" trailed by nothing but function words, introduced by a
WH-question, is never a real command regardless of which specific opinion
phrase (if any) precedes it. This is strictly narrower than the old
lead-in enumeration — it can't fire on "what should we play, some jazz or
rock?" (content survives) or "should I play the new Drake album" (no
WH-word), both already-pinned real-request cases.

As a side effect this also closes an adjacent, previously-unnoticed gap:
"what game should we play" (game-reference word BEFORE "play") wasn't
caught by the existing `_GAME_REFERENCE_WORDS` check either, since that
only inspects the span AFTER the verb.

### Validation

- `tests/unit/test_routing_rules.py`: the exact prod transcript plus
  paraphrases ("What map should we play on?", "Which map should we play
  on", "What difficulty should we play on?", "Which one should we play
  with?") — all correctly flagged non-music. Confirmed the guard does NOT
  touch real content ("What should we play, some jazz or rock?", "Which
  song should we play, Bohemian Rhapsody?") and stays scoped to the play
  verb's own clause (an unrelated leading WH-question doesn't suppress a
  real trailing command).
- Mutation-verified: disabled the new condition, confirmed the exact prod
  transcript's test failed, restored, confirmed all 124 tests in the file
  pass.
- Full unit suite: 2050 passed, 1 skipped, no regressions.
- Deliberately did NOT add "as" to `_PLAY_SPAN_STOPWORDS` to also catch
  "what should we play as?" — no production evidence for that specific
  shape yet, consistent with this note's own "extend only with evidence"
  discipline. Watch for recurrence.
