---
type: research
status: active
date: 2026-09-08
tags: [voice, wake-word, deepgram, openwakeword, salvage, false-positive]
related: [[deepgram-zombie-stream-no-transcript]] [[wake-utterances-lost-to-deepgram-miss-salvage]] [[wake-gate-stt-mishear-rejection]]
---

# The 294 "Deepgram never delivered a transcript" misses are mostly OpenWakeWord false-positives, not lost commands

## Why

Full-log census (41 days, 152,552 records) initially flagged "Deepgram
stream disconnected" (353×) and "Wake word fired but Deepgram never
delivered transcript" (294×) as the largest failure signature in the whole
log — bigger than every LLM-cascade issue combined. Before treating it as
a crisis on par with [[wake-utterances-lost-to-deepgram-miss-salvage]]'s
original ~19-in-one-session finding, it needed the same rigor: read the
actual content, not just the count.

## Finding

**Salvage success rate: 0 / 294.** Every miss either got rejected by the
text-wake gate (274) or failed the salvage STT call itself (20) — zero
`"Wake utterance salvaged via fallback STT"` events anywhere in the log.
That is the opposite of [[wake-utterances-lost-to-deepgram-miss-salvage]]'s
stated success metric ("the rate of still-eaten requests should drop to
near-zero").

Pulling the actual rejected transcripts explains why — they are not
truncated or mis-transcribed real commands, they are textbook Whisper/
Gemini **hallucinations on near-silent audio**:

```
'the quick brown fox jumps over the lazy dog'
'Hi everyone, welcome to the channel.'
'Woof woof'
'The sound of a cat meowing.'
'Transcribe exactly what is said in this audio. Return ONLY the transcription tex'
```

(The last one is Gemini's own system instruction leaking back as if it
were the transcription — a second-order confirmation of "the model had
nothing to transcribe and confabulated.")

Cross-checking against the full wake-gate decision distribution (44,009
decisions) confirms the mechanism:

| audio_match | text_match | count |
|---|---|---|
| True | True (clean address) | 138 |
| **True** | **False (acoustic-only)** | **4,454** |
| False | True | 211 |
| False | False | 39,206 |

OpenWakeWord fires acoustically-without-text-confirmation **32× more
often** than it produces a clean dual-confirmed wake. The 294-case subset
(where Deepgram delivers literally nothing) is simply the quietest end of
that same false-positive distribution — audio so faint or noise-only that
even Deepgram's real-time listener has nothing to transcribe, and salvage's
STT, fed the same near-silent clip, invents plausible filler rather than
returning empty (a documented Whisper/Gemini behavior, not a bug here).

## What this is NOT

Not evidence that the zombie-recovery or salvage machinery is broken. Both
are functioning exactly as designed: the text-wake gate is *correctly*
rejecting hallucinated non-address content, exactly the loopback/false-fire
protection [[deepgram-zombie-stream-no-transcript]] and
[[wake-utterances-lost-to-deepgram-miss-salvage]] built it for. In the
~40 rejected transcripts read directly, none looked like a genuine
truncated or garbled "Hey Poob, play X" — a real command has enough
signal that even a bad transcription contains *some* content word, and
these don't.

## What this changes

- The "294 lost commands" framing from the initial census pass is wrong.
  The true count of genuinely-eaten real commands is almost certainly much
  smaller — this data doesn't isolate it precisely, but zero of the sampled
  rejections support a large number.
- [[wake-utterances-lost-to-deepgram-miss-salvage]]'s success metric
  ("salvaged=False rate should drop to near-zero") was written assuming
  most misses ARE real commands. Given the false-positive-dominated
  reality, a persistently-0% salvage rate is compatible with the fix
  having ALREADY worked as intended — there's nothing real left to
  salvage in this sample, not that salvage itself is failing.

## The one real lever, deliberately not pulled here

OpenWakeWord's acoustic threshold could be tightened to cut the
32:1 false-positive ratio. Not done in this pass: it's a straight
trade-off against missing quieter genuine wake words, which is a judgment
call for the operator, not something to change on volume-count evidence
alone. If pursued, the right instrument is the `oww_peak` score already
logged on every `Wake gate decision` event (see
docs/incidents/voice-pipeline-cold-start-drops-requests) — plot the peak
distribution of the acoustic-only-false-fire population vs the
dual-confirmed population to find a threshold that would separate them
before touching the constant.

## Follow-ups

- If a genuine "eaten real command" complaint recurs, the actionable next
  step is reading the SPECIFIC salvaged transcript for that incident
  (logged at INFO on both the rejection and the STT-failure path) rather
  than trusting the raw miss count.
- Worth eventually distinguishing, in the miss log line itself, "salvage
  transcribed something incoherent" from "salvage got real content that
  failed the wake-word text gate" — right now both log identically at INFO
  and only reading the `transcript` field tells them apart.
