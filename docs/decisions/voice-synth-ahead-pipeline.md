---
type: decision
status: active
date: 2026-06-11
tags: [voice, tts, latency, pacing]
related: [[voice-architecture]] [[voice-latency-phase2-filler-dispatch]] [[speculative-music-wrap]] [[boob-music-wrap-variant]]
---

# Synth-ahead pipeline for voice replies + length latitude

## Context

Operator report (2026-06-11): Poob's voice replies felt *"snappy, with ~2
seconds of silence between sentences"* — and the sentences themselves were too
short ("longer responses are warranted than super snappy stuff"). Live logs
(2026-06-12 02:30) confirmed both halves on the same response:

```
02:30:33.99  Playing audio  bytes=9260   # sentence 1
02:30:37.05  Playing audio  bytes=3980   # sentence 2  (+3.06s)
```

Two **independent, compounding** root causes — not one.

### Defect 1 — serial synth loop (the silence)

The per-sentence synth+play loop was serial:

```python
async for sentence in respond_streaming(...):
    audio = await self._synthesize(sentence)   # blocks the loop ~1-1.5s
    while self.voice_client.is_playing():       # wait for previous to drain
        await asyncio.sleep(0.05)
    await self._play_audio(audio)
```

Synthesis of sentence N+1 did not begin until the loop body for N completed.
When a short jab plays faster (~0.5-1s) than the next sentence synthesizes
(~1-1.5s TTS round-trip), the consumer sits idle → dead air. Short sentences
*maximize* those boundaries, so terse replies sounded the most choppy.

The loop was also **duplicated across three call sites** —
`_process_single_response` (persona-aware), `_process_utterance`, and
`_drain_pending_utterances`. The latter two called `_synthesize` directly with
**no persona dispatch**, so a `VOICE_TOOB`/`VOICE_BOOB` routing sentinel
arriving through those paths would have been *spoken aloud* as text — a latent
bug.

### Defect 2 — prompt clamp fighting the token budget (the snappiness)

`config.voice_llm_max_tokens = 200  # Allow 2-4 sentences` set the *intent* to
2-4 sentences. But the generation-path prompt rule clamped to *"Keep it tight
in voice. 1-2 sentences, ~1-25 words"* and the architecture note enshrined
"1-2 sentences max". The prompt was contradicting the config's own budget, so
every reply collapsed to a single snap jab regardless of whether the moment
deserved more.

## Decision

**1. One shared synth-ahead pipeline.** `VoiceSession._stream_synth_and_play`
runs a producer/consumer pair over a bounded `asyncio.Queue(maxsize=2)`:

- **Producer** pulls sentences, resolves persona from the `VOICE_TOOB`/
  `VOICE_BOOB` sentinels, synthesizes, and enqueues audio. It races ahead —
  synthesizing N+1 while the consumer is still playing N.
- **Consumer** dequeues and plays in order, waiting only for the previous TTS
  to drain (or overlaying when music is active).

All three response paths now delegate to it. This eliminates the inter-sentence
gap (synth(N+1) overlaps play(N)), fixes the missing-persona-dispatch latent
bug uniformly, and removes the triplicated loop. **First-word latency is
unchanged** — the first sentence still synthesizes and plays immediately; only
N+1 onward is prefetched.

`maxsize=2` bounds look-ahead to two sentences — enough to mask TTS latency
across a burst of short jabs without synthesizing an entire runaway response up
front.

**2. Length latitude over a hard clamp.** The prompt rule becomes *"Match your
length to the moment. Default to tight … but when there's something real to say
… let it breathe to three or four sentences. Read the room … Just don't ramble
into a monologue."* This aligns the prompt with the existing 200-token budget
and follows the agentic-mastery north-star (give the model judgment, not a
hardcoded word cap). The 200-token ceiling stays as the safety rail. The two
fixes compound positively: fuller sentences also play longer, further masking
any residual synth latency.

## Alternatives considered

- **Keep serial, just shorten TTS round-trip (Kokoro/local).** Doesn't fix the
  structural gap — any per-sentence latency > playback still leaves silence.
  Orthogonal; see [[voice-latency-phase3-kokoro]].
- **Synthesize the whole response, then play.** Kills the streaming first-word
  latency the speculative-wrap path depends on ([[speculative-music-wrap]]).
  Rejected.
- **Only relax the prompt, leave pacing.** Longer sentences *mask* the gap but
  don't remove it; a genuinely short reply ("nah.") would still stutter. Both
  were real defects; both fixed.
- **Unbounded look-ahead queue.** Would synthesize an entire response up front
  on a long reply, wasting TTS calls if the user interrupts. Bounded at 2.

## Consequences

- Inter-sentence silence is gone for normal replies and minimized for
  back-to-back short jabs.
- `_process_utterance` / `_drain_pending_utterances` gain correct Toob/Boob
  dispatch and the music-overlay check they previously lacked.
- Replies can now run 3-4 sentences when the model judges it worthwhile.
- The synth+play loop exists in exactly one place; re-forking it is caught by
  the grep-as-test in `tests/unit/test_voice_conversation_pacing.py`.

## Validation

`tests/unit/test_voice_conversation_pacing.py`:
- `test_next_sentence_synthesizes_while_current_plays` — asserts synth(2) is
  issued while play(1) is still blocked (would fail on the old serial loop).
- persona-sentinel routing + not-spoken; falsy-audio skip; all-three-paths
  delegation; prompt latitude present, rigid clamp gone.
