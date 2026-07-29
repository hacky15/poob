---
type: research
status: active
date: 2026-07-29
tags: [voice, music, audit, latency, loopback, junk-results]
related: [[music-search-candidate-rerank]] [[music-tool-call-robustness]] [[wake-word-dual-gate]] [[wake-address-hey-dropout]] [[wake-gate-over-rejection-during-music]] [[per-subsystem-component-logging]]
---

# Session audit 2026-07-29 — three reported symptoms, one shipped fix

First audit run against `poob.jsonl` rather than by grepping `voice.log`.
The component-filtered structured log made this take minutes instead of an
hour, and made the counts below trustworthy rather than grep-approximate.

Operator report: *"mostly functional, some slow responses, one double
response that I noticed."* All three reproduced in the logs. Only one
warranted a code change — the reasoning for **not** changing the other two is
the substance of this note.

## Window

`2026-07-27T16:44Z → 2026-07-29T04:22Z`, build `131fa6f` (PR #5).
5,478 structured events — **1 error, 52 warnings**. The single error was a
voice WS 4000 that auto-reconnected.

## 1. Double response — REPRODUCED, deliberately NOT fixed

```
02:57:49  addressed   "Poob. Play Diddy Heilett. Stop"        <- STT garbled the name
02:57:50  tool_route  {'query': 'Diddy Heilett', 'action': 'play'}
02:57:52  addressed   "Hey, Poob. Play Diddy Heil Epstein."   <- user re-says it, +2.1s
02:57:55  Response complete  "You settle for the plebeian drivel..."
02:57:56  tool_route  {'query': 'Diddy Heil Epstein'}
02:57:57  music.response  Queued "Diddy Trial Explained…[8:50]"   <- junk, from request 1
02:57:58  music.response  Queued "Diddy Heil Epstein…[3:23]"      <- correct, from request 2
02:57:59  Response complete  "you've sunk lower than a diseased…"
```

Two spoken replies and two queued songs. Exactly one occurrence; the FIFO
queue engaged exactly once in the whole window (`Queued response (Poob busy)`
n=1).

**Mechanism, confirmed in code:** `_respond_to_transcript` queues an
addressed utterance when `_response_lock` is held rather than dropping it,
then drains FIFO. Both utterances were genuinely addressed, from the same
user, 2.1s apart. Poob answered both — *as designed*.

**Why dedup did not catch it:** `_is_duplicate_play`
([[music-tool-call-robustness]]) keys on the **normalized query string**
within 20s. `"Diddy Heilett"` != `"Diddy Heil Epstein"`, so it correctly
declined. That design targets *verbatim* retries; a **garbled-then-corrected**
retry is a different shape it never claimed to cover.

**Why no fix shipped.** Every candidate creates a worse failure than the one
it solves:

- *Fuzzy/similarity dedup* — would have to treat two different strings as the
  same intent. `"play Diddy Heil Epstein"` vs `"play Diddy Trial"` are
  genuinely different requests; so are `"play X"` and `"play X remix"`. This
  is the open-ended-similarity-over-natural-language trap that
  [[play-question-misrouted-to-play-command]] broke three times.
- *Supersede the in-flight request on same-user re-address* — silently drops
  `"play X"` followed 2s later by `"skip"`, a normal pairing.
- *Stop queueing* — reintroduces the dropped-request problem the queue exists
  to prevent.

One occurrence, correct-by-design behavior, and no candidate fix that does not
manufacture a new bug. **Needs more evidence before touching.** If it recurs,
the discriminator worth exploring is "same user, same action, overlapping
in-flight, query is a near-substring of the newer one" — narrow enough to be
safe, but not justified by n=1.

## 2. Junk music results — FIXED

2 of 6 song queues were non-music, both **under** the 900s longform gate, and
one carries behavioural proof of rejection:

```
02:57:57  Queued "Diddy Trial Explained: What you need to know"      [8:50 = 530s]
03:07:47  Queued "Joe Rogan REVEALS Why The Inmates Attacked Diddy…" [12:19 = 739s]
03:09:15  Skipped "Diddy Trial Explained: What you need to know"      <- user skipped it
```

Exactly the sub-900s gap the 2026-07-17 addendum to
[[music-search-candidate-rerank]] exists for. Neither title hit an existing
marker. Added two, evidence-derived one per observed failure:

- `"what you need to know"`
- `"reveals why"`

**Multi-word, per that note's own discipline.** The shorter candidate
`"you need to know"` was tested and **rejected**: it swallows *"Everything
You Need To Know About Love"*. That is the same trap the note already records
for `"how to"` → *How to Save a Life*.

**Why this is low-risk:** a junk marker never hard-rejects. It triggers the
widened `ytsearch5` re-rank and applies a −0.6 score penalty; the pass-1 hit
still competes with an incumbent bonus (the additive guarantee). A false
positive costs ranking, not the result.

**What this fix does NOT promise.** It guarantees these two titles are
penalised and the search widens. It does **not** guarantee the replacement is
better — that depends on YouTube's top-5 for the query, which cannot be
verified offline. For a bare-surname query about someone currently in the
news, the whole candidate pool may be news.

## 3. "Slow responses" — REPRODUCED, and it is not latency

`total_ms` p50 **5067ms**, max **6656ms** — which looks alarming until
decomposed:

```
+0.00s  addressed
+0.03s  filler audio playing          <- first sound in 30ms
+0.29s  LLM first sentence (285ms)
+1.71s  TTS synthesized (both chunks)
+2.01s  real speech starts            <- ~2s to first real word
+5.05s  chunk 2 starts
+6.60s  Response complete (total_ms=6597)
```

**`total_ms` includes speech playback duration.** LLM p50 is 826ms (max
1513ms); TTS lands inside 2s. The remaining ~4.5s is Poob *talking*.

So the system is responsive; the replies are long. Making `total_ms` smaller
means making Poob more terse — a persona/product decision, not a performance
fix. Recorded here because the metric invites a wrong conclusion, and the
next person reading a 6.6s number will reach for the wrong lever.

**Suggested follow-up (not done):** emit a separate `first_speech_ms` so
responsiveness and verbosity stop sharing one number.

## 4. Loopback guard ate 3 real commands — REPORTED, deliberately NOT fixed

Every rejection last night was a genuine request:

```
x2  "Hey, Poob. Play Hyper Vader."
x1  "Hey, Poob. Play hyperbater."
```

All hit the third branch of the dual gate: text matched, `_bot_audio_active`
(now `_is_speaking` only, per [[wake-gate-over-rejection-during-music]]), and
openwakeword did not acoustically confirm. The users spoke over Poob and got
silence.

**Why no fix on this evidence.** This gate has been retuned **four times in
both directions** ([[wake-word-dual-gate]],
[[wake-gate-over-rejection-during-music]], [[wake-gate-stt-mishear-rejection]],
[[wake-address-hey-dropout]]), and the obvious loosening is explicitly
rejected with numbers in [[wake-address-hey-dropout]]:

> *"The tempting fix — trust openwakeword and respond whenever it fires — was
> rejected. It would have made Poob blurt at all 468 of the false fires the
> text gate currently catches… weakening it trades 6 missed commands for
> hundreds of unprompted interruptions."*

I have **3 data points** and no measurement of how many false fires the gate
suppressed in the same window. Loosening a gate with that history on n=3 is
how the inverse incident gets written.

**The idea worth evidence-gathering:** the genuine discriminator is *content*,
not timing — real loopback is a delayed copy of Poob's **own recent TTS text**,
and none of these three matched what Poob was saying. A "does this transcript
echo my recent speech" check would be tighter than "bot audio active". That
needs a counterfactual false-fire count before it is worth building.

## Also seen (no action)

- `Salvage STT failed` x2 — first occurrences; the salvage path erroring
  rather than rejecting. Both same user. Watch; not yet a pattern.
- `Playback error` x1, `Tool detection failed, trying next` x1.
- 15 `Wake word fired but Deepgram never delivered transcript`, all with a
  preceding `Salvaged transcript rejected` — the salvage system correctly
  refusing openwakeword false-fires, consistent with three prior audits.

## Method note

Grepping `voice.log` for `error|critical` in earlier audits produced false
hits from the word *"critical"* inside user speech. Filtering
`poob.jsonl` on `level` gives an exact count. The observability work shipped
in PR #4 paid for itself in the first audit that used it.
