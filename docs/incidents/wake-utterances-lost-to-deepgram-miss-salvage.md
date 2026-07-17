---
type: incident
status: resolved
date: 2026-07-17
tags: [voice, deepgram, stt, reliability, dual-pipeline, salvage]
related: [[deepgram-zombie-stream-no-transcript]] [[wake-word-dual-gate]] [[voice-cpu-starvation-on-shared-host]]
---

# Every Deepgram wake-miss still ATE the request — salvage the utterance via fallback STT

## Symptom

Full-log census (2026-07-08 → 07-17): `Wake word fired but Deepgram never
delivered transcript` is the **single most frequent user-visible failure** —
**~19 occurrences in the 2026-07-17 session alone**, spread across six
different users, on the freshly-deployed code. Each one is a person saying
"Hey Poob …" and getting **nothing**: no reply, no music, no error. This is
the largest single contributor to the operator's "it's so on and off — it
works and then it doesn't" complaint: the same request works one minute and
is silently eaten the next, depending on per-user Deepgram stream health.

## Root cause

Three generations of zombie-stream fixes
([[deepgram-zombie-stream-no-transcript]] + its two addenda) each improved
**detection and stream recovery** — but the design has always **dropped the
wake-fired utterance itself**. `_deferred_emit` waits 1.5 s for the
transcript, counts the miss, (at 2 consecutive misses) force-reconnects the
stream — and returns. Recovery only helps the *next* attempt. The current
request is gone. The vault's own open-edge note said it plainly:
*"user-visible failures ARE the detector."*

Frequency is also worse than the original incident assumed: misses now
appear scattered across many users per session (single `zombie_recovery=False`
misses that never reach the threshold), consistent with transient per-stream
latency (>1.5 s delivery) as well as true zombies — both of which still eat
the utterance.

## Fix — salvage the utterance, keep the recovery

The raw audio was always in our hands; only the transcription channel
failed. `DualPipelineProcessor` now keeps an **always-on per-user salvage
ring** of the downsampled 16 kHz mono PCM (`_SALVAGE_MAX_FRAMES = 1000` ≈
20 s — utterances force-emit at 15 s, so the ring provably covers any
utterance; ~640 KB/user ceiling). On a `_deferred_emit` miss, after the
existing zombie accounting, `_attempt_salvage`:

1. Slices the ring from `speech_start_time − 0.75 s` onward (timestamped
   frames); skips if under 0.4 s of audio (spurious wake blip).
2. Transcribes it via the **session's one-shot STT cascade**
   (`VoiceSession._transcribe_16k` — the same `GroqWhisperSTT → Gemini →
   Deepgram-REST → local-whisper` stack the non-dual path uses, bound to
   16 kHz), 4 s timeout, never raises.
3. **Re-imposes the text wake gate** on the salvaged transcript
   (`_text_wake_word_match`). Load-bearing: the acoustic wake false-fires on
   music/loopback (documented in [[wake-word-dual-gate]]), and Deepgram
   delivering nothing is precisely the case where the primary transcript
   gate never ran — without this check, salvage would transcribe mic-loopback
   audio and execute song lyrics as commands.
4. Emits through the standard `_do_emit` path (dedup and downstream
   handling identical to a normal addressed utterance).

The zombie-miss counter is deliberately **not** reset on salvage success —
salvage is the ambulance, not the cure; the stream is still sick and
`force_reconnect` should still fire on the next miss.

Net: a silently-eaten request becomes a served request ~1–2 s late, while
stream recovery continues to work exactly as before. Disabled (pre-salvage
behavior) when no `salvage_transcriber` is wired.

## Validation

- `tests/unit/test_dual_pipeline.py` (7 new): headline rescue path (emitted
  as addressed, miss still counted); text-gate rejection of lyric-like
  salvage (the loopback hole stays closed); disabled-without-transcriber;
  too-little-audio skips the STT call; STT failure swallowed; ring slice
  excludes stale frames; empty transcript no-op. Full dual-pipeline suite
  green (30).
- Post-deploy signals: `Wake utterance salvaged via fallback STT` on rescues;
  the miss warning now carries `salvaged=True/False`. Success metric: the
  rate of `salvaged=False` misses (still-eaten requests) should drop to
  near-zero except when the salvage STT itself is down.

## Follow-ups

- Deepgram latency vs. true-zombie split is now measurable: a salvage whose
  transcript matches a Deepgram transcript that arrives 2–3 s late would
  indicate latency (possibly shared-host CPU — see
  [[voice-cpu-starvation-on-shared-host]]), not zombies. Worth an audit if
  salvage rates stay high.
- The passive keepalive-side zombie detector remains unbuilt (deliberate —
  see the original incident's follow-up); salvage lowers its urgency further.
