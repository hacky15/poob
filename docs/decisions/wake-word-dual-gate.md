---
type: decision
status: active
date: 2026-04-21
tags: [voice, wake-word]
related: [[wake-gate-over-rejection-during-music]] [[voice-architecture]]
---

# Wake-word gate — context-aware acoustic + text confirmation

## Context

Openwakeword fires on acoustic signal; Deepgram produces a streaming transcript regex-matched against "hey poob". Using either alone is insufficient:

- **Text-only** accepts Deepgram hallucinations. A song with "blah blah blah" lyrics + keyterm-biased STT produced a phantom "Hey Poob" and queued a duplicate track with no user speech at all.
- **Acoustic-only** rejects clean wake words in noisy environments — openwakeword is not reliable enough on its own when game audio, TV, or crosstalk is underneath.

## Decision

**Dual-gate with a context-aware relaxation** when the bot is silent.

```python
if text_match and audio_match:
    addressed = True
elif text_match and not bot_audio_active():
    addressed = True     # bot silent → no loopback risk → trust transcript
elif text_match and bot_audio_active() and not audio_match:
    addressed = False    # bot producing audio, likely mic loopback
else:
    addressed = False    # no text match
```

`bot_audio_active` is a callback the session provides. It returns True only when **Poob is TTS-speaking** (`self._is_speaking`). Music playing alone does NOT count — music tracks don't contain wake-word phonemes and aren't a loopback risk. See [[wake-gate-over-rejection-during-music]] for the incident that narrowed the condition.

## Failure classes covered

- User says "Hey Poob" cleanly → both signals → addressed.
- User says "Hey Poob" in noise, acoustic misses → text-only, bot silent → addressed (correct).
- Music/TTS loopback through mic, Deepgram hallucinates "Hey Poob" → text-only while bot TTS → rejected (correct).

## Alternatives considered

- **v1: both gates required, unconditional** (April 21). Rejected real-world legitimate wake words whenever openwakeword missed. Superseded the same day.
- **Acoustic cooldown after TTS.** Magic timer, brittle. Dropped.
- **Semantic confirmation via LLM.** Too slow to run on every utterance.

## Consequences

- A user saying "Hey Poob skip" while Poob is mid-TTS AND openwakeword misses → rejected. Acceptable: when the bot is actively speaking the user can also click the persistent skip button on the now-playing embed, and openwakeword usually fires on clear speech even with some background audio.
- Any signal loop where the bot produces audio that a mic can capture creates this class of bug. Keyterm biasing in the ASR amplifies it — primed words hallucinate from weak signals. If we ever expose other bias-prone keyterms (deal names, brands) the same symmetric dual-gate rule applies: the semantic layer is confirmation, not primary.
