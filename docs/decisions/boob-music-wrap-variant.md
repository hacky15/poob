---
type: decision
status: active
date: 2026-05-06
tags: [voice, music, toob, persona, ffmpeg]
related: [[toob-voice-filter-chain]] [[speculative-music-wrap]] [[voice-architecture]] [[poobbrain-architecture]]
---

# Boob — rare friendly music-wrap variant ("Toob's side piece")

> **2026-06-03 retune:** rarity dropped from 1 in 20 (`0.05`) to **1 in 75** (`1 / 75 ≈ 0.0133`) at the user's request — the variant was surfacing too often to feel special. Only `BOOB_PROBABILITY` and the test range guard changed; the architecture below is otherwise unchanged. Probability figures in this note reflect the current 1-in-75 target.

## Context

Toob ([[toob-voice-filter-chain]]) is the only voice that wraps music plays — a dark, menacing, single-sentence mock of every request. Funny on first hit, but with one persona on every play the bit becomes predictable. User asked for a rare opposite-vibe variant: same Toob lineage, but sweet, complimentary, three-sentence delivery, and the variant must self-introduce as "Toob's side piece" so the relationship lands every time. Original target was "~1 in 20"; retuned to "~1 in 75" on 2026-06-03 (see note above).

## Decision

Add **Boob** as a second music-wrap persona, gated by a ~1.3% (`1 / 75`) random roll inside the existing voice + `action=play` branch. Mirrors Toob's architecture end-to-end — different filter chain, different TTS voice, different prompt — so the choice between Toob and Boob is a one-bit dispatch and nothing else regresses.

### Trigger

`brain/poob.py:respond_streaming` for the music-play branch:

```python
is_boob = random.random() < BOOB_PROBABILITY  # 1/75 ≈ 0.0133
yield VOICE_BOOB if is_boob else VOICE_TOOB
async for sentence in self._handle_music_voice_streaming(
    ..., persona="boob" if is_boob else "toob",
):
    yield sentence
```

`BOOB_PROBABILITY = 1 / 75` lives next to `VOICE_BOOB`. Range guard test in
`tests/unit/test_brain_boob_variant.py` keeps it in [0.01, 0.02] — too high
burns the rarity, too low means nobody ever hears it.

### Voice signal

`VOICE_BOOB = "__VOICE_BOOB__"` joins `VOICE_POOB` and `VOICE_TOOB` as the
structural sentinel yielded as the first item from `respond_streaming`.
The session loop dispatches synth via a string-keyed table:

```python
synth_dispatch = {
    "poob": self._synthesize,
    "toob": self._synthesize_toob,
    "boob": self._synthesize_boob,
}
```

This replaces the previous `use_toob_voice: bool` flag — adding a third
persona via another bool would have produced the wrong combinatorics. The
table extends cleanly if a fourth persona is ever added.

### Filter chain — inverse of Toob

```
asetrate=28000, aresample=24000,
vibrato=f=6.5:d=0.10,
volume=1.1
```

Stage rationale:

- **`asetrate=28000`** on a 24kHz source pitches UP ~+2.7 semitones (factor 1.17). Brighter, more feminine, slightly faster duration — the audible inverse of Toob's `asetrate=18500` pitch drop.
- **`aresample=24000`** restores playable rate after the pitch shift.
- **`vibrato=f=6.5:d=0.10`** — slightly higher frequency than Toob's 5.5 Hz, half the depth (0.10 vs 0.15). Reads as cheerful prosody instead of menacing wobble.
- **No `atempo`** — asetrate's natural ~17% speed-up is the right bump. Chaining atempo would chipmunk the audio.
- **No `bass`, no `aecho`** — Toob's bass boost and cavernous reverb are precisely the things Boob is the opposite of. Removing them produces an intimate, dry, close-mic'd sound that lands as warm rather than thunderous.
- **`volume=1.1`** — light pre-gain. `speechnorm` in `_play_audio` does the heavy lifting; this just keeps the chain from leaving us under-loud.

### TTS voice

Google Chirp3-HD **`en-US-Chirp3-HD-Leda`** (female), `speaking_rate=1.05`. Distinct from Poob's Fenrir (male) and Toob's Enceladus (male, deeper). The filter chain works on any audio, so a TTS-cascade fallback (Edge TTS, Kokoro) still delivers Boob's character — just with less voice-quality polish.

### Prompt — three sentences, side-piece intro

`_stream_boob_wrap_from_query` mirrors `_stream_toob_wrap_from_query` (same Groq `llama-3.1-8b-instant` streaming path) but the system prompt enforces:

1. Sentence one MUST introduce as Toob's side piece. Phrasing varies — `"Hey, Boob here, Toob's side piece"`, `"Boob speaking, Toob's side piece"`, etc. — but the relationship to Toob lands every single time.
2. Three sentences total, ~30-50 words. Hard floor: longer than Toob's one-liner is the whole point of the variant.
3. Compliment the user's *taste* (genre, mood, vibe), not Toob-style mockery. Pure positive valence.

`max_tokens` capped at 200 — enough room for three sentences without
runaway monologue.

### FFmpeg prewarm

`_prewarm_ffmpeg` now exercises both `_TOOB_FILTER_CHAIN` and
`_BOOB_FILTER_CHAIN` at module import. The Boob chain has a different
shape (no atempo, no aecho, no bass), so its filter-graph init path is a
separate cold-start. Without prewarming, the first ~1-in-20 hit would
pay ~700ms of cold-start latency the one time someone actually triggers
it. Prewarm cost is amortized into container-boot time nobody waits on.
(Cold-start avoidance matters more now that a Boob hit is ~1 in 75 — the
prewarmed graph is the only thing keeping that rare hit from also being slow.)

## Alternatives considered

- **Persona as a `bool` (`is_boob`) instead of a string.** Only works for two personas. The third persona Poob (regular casual voice) was already implicit in the `else` branch; making the dispatch table explicit makes the contract obvious and prevents the next persona swap from silently breaking the routing.
- **Single shared `_stream_persona_wrap_from_query` with a persona arg.** Smaller diff, but the prompts differ enough (Toob's one-sentence venom vs Boob's three-sentence intro+compliment+send-off) that the shared function would be 80% conditionals. Two methods is cleaner.
- **Reuse `_synthesize_toob` with a filter-chain override.** Same critique — the persona-specific TTS voice (Leda vs Enceladus) and `speaking_rate` differ, so the override would touch most of the function anyway. Two methods, prewarm both, ship.
- **Higher / lower probability.** Originally 5% ("~1 in 20"); retuned to ~1.3% ("~1 in 75") on 2026-06-03 because 1-in-20 surfaced often enough to lose its surprise. Going much lower (≪1%) would make the variant effectively invisible; higher (10%+) and it stops feeling rare.

## Consequences

- **Music-play voice path now has two outcomes.** Toob (~98.7%) is unchanged. Boob (~1.3%, 1 in 75) is the new branch. Skip / pause / stop / volume are unaffected — those are silent and never trigger either persona.
- **Routing, music handler, deal agent, casual chat — all unchanged.** Boob lives entirely inside the existing `_handle_music_voice_streaming` + session-synth surface. No new handler types, no new tool definitions, no LLM model swaps.
- **Slightly larger TTS surface area on the homelab.** Two new FFmpeg invocations per ~75 plays (Boob synth) and one new voice in Google's Chirp catalog. Negligible.
- **Failure mode**: if Boob's TTS or FFmpeg fails, raw audio falls back the same way Toob does. If the wrap LLM call fails, the speculative-wrap path's existing `Speculative wrap failed` log line fires (now with `persona=boob` tag) and music still plays — just without the wrap. Acceptable.

## Validation

- 8 new unit tests in `tests/unit/test_brain_boob_variant.py`:
  - `BOOB_PROBABILITY` in (0, 0.10] range, banded to [0.01, 0.02] around the 1-in-75 target
  - VOICE_BOOB sentinel emitted when `random.random() < BOOB_PROBABILITY`
  - VOICE_TOOB emitted otherwise
  - Wrap call uses ≥100 max_tokens (3-sentence budget)
  - System prompt contains "side piece" + three-sentence directive + positive-valence anchor
  - Wrap streams ≥2 sentences end-to-end
  - Skip / pause / stop never emit VOICE_BOOB
  - Deal route never emits VOICE_BOOB
- All 32 brain tests pass (8 Boob + 9 casual-fallback + 15 multi-guild).
- Full unit suite: TBD until completion.

Subjective in-prod check: at 1 in 75, Boob is now genuinely rare — expect ~1 surfacing per ~75 plays, so this is a long-tail observation rather than a quick repeat test. Each Boob hit should:
- Open with the side-piece intro
- Continue for three sentences of compliment/send-off
- Audibly be a brighter, slightly faster voice than Toob
- Music start after the wrap completes (deferred-playback unchanged)

## Logging signals

- `voice="boob"` in the existing `First sentence ready` log line (was previously only `"poob"` or `"toob"`).
- `Boob TTS synthesized source=<google_leda|cascade_fallback>` per Boob synth.
- `Boob FFmpeg failed/timeout` on filter-chain error (rare).
- `Speculative wrap failed persona=boob` on wrap-LLM error.

## Rollback

Two-line revert in `respond_streaming`'s play branch — set `is_boob = False`, drop the `VOICE_BOOB` yield. Helpers, filter chain, synth function, prewarm step can stay; they're inert without the dispatch.

## Architectural integrity

- **Speculative music wrap** ([[speculative-music-wrap]]) — preserved. Boob streams its wrap concurrently with `ytdl` exactly the way Toob does. The deferred-playback handoff in the session is persona-agnostic.
- **Toob filter chain** ([[toob-voice-filter-chain]]) — unchanged. Both chains live as module-level constants, both prewarmed.
- **Voice signal protocol** — extended cleanly. The structural sentinel pattern continues; no string parsing, no regex, no markers in the spoken text.
- **Multi-guild isolation** ([[multi-guild-isolation]]) — preserved. The Boob roll is per-call; no per-guild persistence; nothing cached.
