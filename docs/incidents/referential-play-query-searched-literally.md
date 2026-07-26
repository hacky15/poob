---
type: incident
status: resolved
date: 2026-07-22
tags: [brain, music, routing, query, stt, safety-net]
related: [[play-question-misrouted-to-play-command]] [[music-search-candidate-rerank]] [[ytdl-search-best-guess-fallback]] [[music-failure-census-2026-07-17]] [[tool-hallucination-from-passive-context]]
---

# "Play the music that we told you to play" → Gorillaz, then Shannon

## Symptom

2026-07-22 01:20, voice, two junk queues 25 seconds apart. STT clipped the
first utterance mid-sentence:

```
01:20:13  wake word addressed  text="Poob, do you wanna actually play the music that we"
01:20:14  Safety net caught missed music intent  query="the music that we"
01:20:16  music.response  Queued Gorillaz - Its the music that we choose [3:32]
```

The user retried, completing the sentence. The router's escalated rung took
it literally too:

```
01:20:38  wake word addressed  text="Hey, Poob. Do you wanna actually play the music that we told you to play? No. I'm gonna"
01:20:39  poob.tool_route  provider=gemini  args={'action':'play','query':'the music that we told you to play'}
01:20:41  music.response  Queued Shannon - Let The Music Play (Official Music Video) [3:39]
```

Users, in the room, on the log:

```
01:20:30  "Oh my god. It's the that one song."
01:20:34  "Which song is this?"
01:20:53  "is supposed to be that Jack Johnson song?"
```

Audit context: **8 play requests in the 07-21→07-22 window, 4 clearly
wrong** — these two, plus `"Sing me a song about Doctor Pepper flavors"` →
a Dr Pepper taste-test video, plus the already-fixed
[[play-question-misrouted-to-play-command]].

## Root cause

`_play_span_content_tokens` answers "does this span name something
searchable?" by subtracting filler from the span's tokens and testing for
emptiness. Empty → blank the query → the empty-query gate asks *"Play
what?"*. That guard is census fix #6 and it works — for `"Play the song.
Fuck."`.

It was one word short. Traced against the real module:

```
span 'the music that we'                    -> tokens {the, music, that, we}
  minus _PLAY_SPAN_STOPWORDS                -> {'we'}      <-- non-empty, survives
span 'the music that we told you to play'   -> {'to','told','we'}   <-- survives
```

`_PLAY_SPAN_STOPWORDS` enumerated the pronouns that appear when **addressing**
Poob (`you`, `me`, `us` — "can you play me…") and never the ones that appear
when **referring back** (`we`, `they`, `i` — "the music that *we* asked
for"). A span whose only survivors are function words *points at* a song; it
does not *name* one.

The second-order effect is what made it land so badly: a pure-function-word
query does not fail loudly, it **succeeds into the wrong song**, because
English function words are exactly what pop titles are made of.

**Structural cause — why this class kept recurring.** The same concept
existed as **three divergent copies**: module-level `_PLAY_SPAN_STOPWORDS`
(24 words) and two verbatim inline `_STOPWORDS` duplicates in the text and
voice music handlers (27 words each, carrying `or`/`to`/`for` that the
module set lacked, plus their own local `import re as _re` and tokenizer).
Evidence that improved one never reached the others. This is precisely the
"a value flows through N stages and gets lost — fix where it drops, don't
patch every stage" smell CLAUDE.md names.

## Fix

**One lexicon, two closed tiers, one tokenizer.** The inline duplicates are
deleted; all call sites use `_play_span_content_tokens`. The lexicon gains
the closed function-word class: pronouns, auxiliaries/modals, prepositions,
conjunctions, wh-words, and speech-act verbs (`told`/`asked`/`want`).

**Why enumeration is legitimate here, when it failed three times in
[[play-question-misrouted-to-play-command]]:** that note's lesson is that
*verb and opinion PHRASES* are an open set — paraphrase space is infinite,
so v1/v2/v3 each broke real requests. Function words are a genuine **closed
class**: finite, stable, and impossible to paraphrase into existence.

**Why [[ytdl-search-best-guess-fallback]]'s objection does not transfer.**
That note explicitly rejected stripping modifier words because it *"bakes
English-grammar assumptions into a global music search"* — `"to"` is a stop
word in English but part of real transliterated titles. That objection is
about **editing the query text**. This gate never edits: the query is sent
**whole** or **blanked entirely**. A non-English title is only affected if
it consists *exclusively* of English function words. `"Ambatakam to
Marwani"` keeps `{ambatakam, marwani}` and searches unchanged.

The gate also **fails open** — unknown tokens count as content — so
`"tiki tiki"`, `"cheeky cheeky"`, and `"body dee dum"` are untouched.

**Deliberately excluded: `one` and `something`.** Both are real titles
("One" — U2/Metallica), and `poob.py` already kept `one` out of the span
stopwords for exactly this reason. Cost: `"play the one from before"` still
searches literally. Accepted and pinned.

## Validation

`tests/unit/test_routing_rules.py`:

- both production spans reduce to empty and blank end-to-end (`query == ""`)
- 22 real queries keep content — `jazz`, `tiki tiki`, `John Coltrane`,
  `Bohemian Rhapsody`, `Ambatakam to Marwani`, `we are young`,
  `she loves you`, `i told you so`, `return to sender`, …
- `"the song"` / `"some music"` still blank (census fix #6 preserved)
- `one` / `something` still searchable (documented trade-off, pinned)
- all-pronoun titles blank (documented trade-off — `"you and me"` already
  behaved this way **before** this change; the class is not new)
- a source-level assertion that no inline `_STOPWORDS` set ever reappears

Full unit suite green.

## Follow-ups — the part this fix does NOT solve

The gate stops junk from being *searched*. It does not make Poob *understand*
the request; the user now gets *"Play what?"* instead of Gorillaz. Better,
not good. All three remaining 07-22 failures are the same unsolved shape —
**Poob literal-searches descriptions of songs instead of titles**:

- referential — "the music that we told you to play"
- superlative — "Jack Johnson's number one hit of all time" → *One Step
  Ahead* (users disagreed out loud: *"upside down"*)
- generative — "Sing me a song **about** Doctor Pepper flavors" → a
  taste-test video

Resolving a description to a title is an LLM job, not a deterministic one —
the router knows Jack Johnson's biggest hit. That is a prompt/tool-description
change, and `MUSIC_TOOL` is at 1352/1400 tokens, so it needs the
de-duplication noted in `test_music_tool_stays_under_budget` first. Tracked,
not attempted here — evidence-first, and this fix stands on its own.

Open-class referential phrasing (`"what we were listening to"` — `listening`
survives) is likewise out of scope by design. Extend only with production
evidence.
