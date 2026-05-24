---
type: decision
status: active
date: 2026-05-24
tags: [voice, latency, filler-audio, ux]
related: [[voice-latency-optimization]] [[voice-architecture]] [[voice-latency-phase3-kokoro]] [[music-player-architecture]]
---

# Filler audio actually fires — Phase 2 wake-up

## Context

Phase 2 of [[voice-latency-optimization]] (filler audio) was built but never dispatched. `FillerPlayer` was instantiated in `VoiceSession.__init__`, populated with the MP3 clips from `generate_fillers()` at startup, and then... nothing. Zero call sites for `filler_player.get_filler_bytes()` or `filler_player.get_random_filler()` existed in the response path. The "cheapest UX win" from the latency plan ("buy 1-2 seconds of perceived latency by playing 'hmm let me think...'") was dead code.

Surfaced during the "what's actually unfinished" audit: `grep -rnE "\.filler_player\."` returned only the constructor + the assignment site in `main.py`. The plan called the FillerPlayer wiring complete in Phase 2 but the consumer never landed.

## Decision

Add `VoiceSession._maybe_play_filler()` and call it via `self._loop.create_task(...)` at the start of `_process_single_response`, inside the `_response_lock` critical section but BEFORE prompt-building + LLM dispatch. The filler runs concurrently with the actual LLM + TTS pipeline; the real response queues behind the filler via the existing `voice_client.is_playing()` gate at the response-playback site.

```python
async with self._response_lock:
    filler_task = self._loop.create_task(self._maybe_play_filler())
    self._inflight_tasks.add(filler_task)
    filler_task.add_done_callback(self._inflight_tasks.discard)
    ...  # prompt build, LLM call, TTS, _play_audio
```

`_maybe_play_filler` is fire-and-forget — no `await` from the caller. Three layers of soft-failure:

1. `filler_player.available` is False (no clips generated at startup) → return silently.
2. `get_filler_bytes()` returns empty/raises → return silently.
3. `_play_audio()` errors → swallow + debug-log.

Routes through the same `_play_audio` method TTS responses use, so:
- Music ducks identically (overlay mixer).
- The real response queues behind via the normal `voice_client.is_playing()` mutex.
- speechnorm filter applies identically.

Task is tracked in `_inflight_tasks` so `cleanup()` cancels it on disconnect.

## Alternatives considered

- **Fire from `_on_dual_addressed`** (before the response task is scheduled). Lower latency — fires <50 ms after end-of-speech — but might fire when the response itself errors mid-build, producing a "hmm..." followed by silence. Rejected for the v1 ship; revisit if 200 ms matters more than the cleaner "filler only fires when real response is coming" guarantee.
- **Inline await instead of create_task.** Would block prompt-building behind the filler synthesis, wasting the entire latency-masking point. Rejected.
- **Pick the filler clip based on transcript content** (e.g., "good question" for actual questions, "alright" for commands). Adds a classifier hop. The clips are short and the user hears one of ~8 randomly chosen ones — the variety is the win. Stays random.
- **Cross-fade from filler to response** (50-100 ms blend). The plan mentions this but the existing `_play_audio` mutex doesn't support it without a bigger refactor. Filler ends, brief silence, response starts. ~50-150 ms gap; acceptable.

## Consequences

- ✅ Phase 2 is alive. The "Hmm...", "Let me think...", "Good question..." clips actually play within ~50-100 ms of end-of-speech on addressed utterances.
- ✅ No changes to the response path's structure — `_play_audio` queues filler then response naturally via the existing mutex.
- ✅ Zero new dependencies, zero config flags. Either the fillers were generated at startup (then they fire) or they weren't (then nothing fires; the response path is unchanged).
- ⚠️ Roughly 50-150 ms gap between filler end and response start, depending on how long the LLM + TTS took. Plan calls for a crossfade; deferred (see alternatives).
- ⚠️ If the LLM responds extremely fast (sub-200 ms total — happens on a cached / trivial path), the filler may still be playing when the real response is ready. The mutex queues the response so it plays after; net effect is a slightly *longer* response time on the fast path. Acceptable trade — fast-path requests are rare in voice, and the filler at least makes Poob sound less robotic.
- ⚠️ Filler always uses Edge TTS voice baked in `generate_fillers()`. If operator changes `voice_tts_voice` mid-session, the filler keeps using the OLD voice until restart. Document; defer regeneration knob until anyone notices.

## Rollback

Single-line revert: delete the `create_task(self._maybe_play_filler())` call in `_process_single_response`. The method stays, the player stays — only the dispatch goes away. Zero migration risk.

If the filler audio sounds bad / annoying / repetitive at a noticeable rate: empty `FILLER_PHRASES` in [src/poob/voice/fillers.py](../../src/poob/voice/fillers.py) so `generate_fillers()` produces no clips. `filler_player.available` is False; `_maybe_play_filler` short-circuits. Zero code change to enable rollback.

## References

- [[voice-latency-optimization]] — the parent plan; Phase 2 is now ✅ shipped alongside Phase 3.
- [[voice-architecture]] — overall voice subsystem map.
- [src/poob/voice/session.py](../../src/poob/voice/session.py) — the new `_maybe_play_filler` + dispatch.
- [src/poob/voice/fillers.py](../../src/poob/voice/fillers.py) — the unchanged FillerPlayer + generate_fillers.
