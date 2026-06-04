---
type: incident
status: resolved
date: 2026-06-04
tags: [voice, wake-word, dual-gate, deepgram, stt]
related: [[wake-gate-stt-mishear-rejection]] [[wake-word-dual-gate]] [[deepgram-zombie-stream-no-transcript]] [[music-routing-flip-a-coin-false-positive]]
---

# Wake address missed when Deepgram drops the "hey" lead-in ("A Poob play X")

## Symptom

The operator (Ben) addressed Poob in voice — "Hey Poob, play fighting music" and similar — and got **no response**. Others in the channel confirmed his audio was fine; he wasn't cutting out. From his side it looked like Poob had simply gone deaf to him.

This is the inverse of [[deepgram-zombie-stream-no-transcript]] (where the *stream* dies and no transcript arrives at all). Here the transcript arrived fine — it was the **text gate** that rejected a real address.

## Root cause

A 5h production-log audit (`2026-06-04`, ~544 audio wake fires) showed the wake pipeline is working as designed for the overwhelming majority of fires: **474 of 544 were spurious** (openwakeword firing on normal speech with no "hey poob") and were **correctly rejected** by the text gate `_text_wake_word_match` in [voice/dual_pipeline.py](../../src/poob/voice/dual_pipeline.py). That rejection is **load-bearing** — without it Poob would blurt during normal conversation hundreds of times an hour.

The real defect was narrow: the `_TEXT_WAKE_RE` regex **hard-required a literal `hey`** before the poob-variant stem. When Deepgram dropped or mangled the "hey" lead-in, a genuine command address fell through to the spurious bucket and was silently rejected. Six real misses in the 5h window, all of this shape (exact logged transcripts):

```
A Poob play fighting music.
Poob, nightcore,
A Poob, a queue up panda by designer, Reverse, slow dank, or
Apoob, remove nightcore.
Poob, remove the night core.
A Poob. Play try not to get scared.
```

Each is unmistakably "Poob, <do-this>" with the "hey" eaten by STT — "Hey" → "A " / glued into "Apoob" / dropped entirely.

This is the exact failure class diagnosed back on 2026-04-23 in [[wake-gate-stt-mishear-rejection]], which proposed two fixes (Option A phonetic allowlist, Option B command-verb override) and recommended Option B but left it unshipped at the dual-pipeline layer. This incident is that fix landing.

### Rejected alternative: "respond on every audio wake"

The tempting "fix" — trust openwakeword and respond whenever it fires — was **rejected**. It would have made Poob blurt at all **468** of the false fires the text gate currently catches (544 fires − 6 real misses − the ~70 clean "hey poob" hits). The text gate is the thing keeping Poob quiet during normal conversation; weakening it trades 6 missed commands for hundreds of unprompted interruptions. The dual-gate's anti-loopback contract ([[wake-word-dual-gate]]) depends on this gate staying tight.

## Fix

Added a **second, tightly-scoped recognition path** to `_text_wake_word_match` — the existing "hey poob" path is kept verbatim as Layer A. Layer B ("command address") matches only if BOTH hold:

- **`_POOB_OPENER_RE`** — a poob-variant opens the utterance (anchored at `^`), tolerating one leading filler word (`a|uh|um|oh|hey|ok|okay|k`) and/or a single glued filler letter restricted to `(?:a|k)?` so "apoob"/"kpoob" parse as filler+stem but "spoof"/"scoob" cannot. Anchoring at the opener is what stops mid/end mentions ("...fucked up, Poob", "Get Poob out of") from qualifying.
- **`_COMMAND_VERB_RE`** — a word-boundaried imperative verb appears anywhere (`\bplay\b` not `play`, so "playlist"/"PUBG" don't count). Verb set: play, queue, skip, next, stop, pause, resume, unpause, remove, clear, cancel, volume, louder, quieter, lower, raise, mute, unmute, nightcore, slow, slowed, speed, reverb, bass, shuffle, autoplay, kill, restart, replay, repeat, turn.

The stem stays the **narrow** family from the original regex, NOT the broad `address_detector._WAKE_WORDS` set — widening it here would re-open the music/TTS-loopback hole that [[wake-word-dual-gate]] was built to close. The opener-anchor + verb conjunction is what keeps Layer B from firing on conversational "poob" mentions.

**Why deterministic (regex) over an LLM rescue:** the gate runs on every utterance, so it must be sub-millisecond and $0 — an LLM rung is too slow and costs per call. A regex also **can't misfire** in the way an LLM would on "yeah poob wasn't working" or "I think poob should play that" — phrasings that read as commands semantically but are clearly not addresses. The conjunction of a structural opener-anchor and a closed verb list is auditable against the labeled log in a way an LLM's judgment is not.

Layer at which it's fixed: the dual-pipeline text gate, the same layer that emitted the "Audio wake word overridden by text" rejection. Fixing upstream (openwakeword) or downstream (post-routing) would be the wrong layer — the data was present and correct at the gate; the gate's matcher was too narrow.

## Validation

TDD + adversarial regression against the real 5h log:

- **Unit suite** `tests/unit/test_wake_address_matcher.py` — 36 cases (the labeled MUST-MATCH / MUST-NOT-MATCH ground truth plus edge cases), all green. Full unit suite: **1466 passed, 1 skipped** (`test_voice.py` ignored — pre-existing unrelated collection failure).
- **Adversarial regression** — imported the actual `_text_wake_word_match` and ran it over every transcript extracted from the 5h log (474 gate-rejected + 53 gate-accepted):
  - Of the 474 previously-rejected transcripts, **exactly 6 now match** — the 6 real misses above. **Zero** pure-noise or about-poob false positives.
  - Of the 53 previously-accepted, **48 still match**; the 5 no-matches are correct (`Yeah.`, `Got it.`, and three where "play" is conversational mid-sentence with no "poob") — those reached "addressed" at runtime via the audio-model deferred-emit path, not the text matcher. **No regression.**

Production log lines to watch: `Command-address wake word match` (new Layer B hits) vs the existing `Text wake word match` (Layer A). A spike in Layer B firing on non-commands would signal the verb list or opener-anchor needs tightening.

## Follow-ups

- [[wake-gate-stt-mishear-rejection]] is updated to `status: resolved` — Option B shipped here at the dual-pipeline layer, as that note recommended.
- Option A (phonetic near-match allowlist) already landed separately at the `address_detector._WAKE_WORDS` layer; the two layers are complementary, not redundant.
- The `wake_gate.stt_mishear_overridden` structured-log follow-up from the earlier note is partially satisfied by the distinct `Command-address wake word match` line; a counter on overridden-vs-recovered would make the recovery rate measurable.
