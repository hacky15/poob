---
type: decision
status: proposed
date: 2026-04-23
tags: [brain, music, voice, tts, latency, concurrency]
related: [[voice-architecture]] [[music-player-architecture]] [[poobbrain-architecture]] [[toob-voice-filter-chain]] [[voice-pipeline-optimization-prompt]]
---

# Speculative Toob wrap — run music search and wrap in parallel

## Context

Voice music-play was strictly serial: tool-routing → ytdl search (~2-3s) → Toob wrap LLM (~1s) → TTS synth (~1s) → playback. User waited ~6.7s from "hey poob play X" to hearing the first word. The April 2026 voice-pipeline research identified this serial chain as the single largest preventable sink.

Key observation from the research: **the wrap is a function of the user's request, not the search result.** Toob is reacting to the user *asking* for a song, not to the specific track metadata. So the wrap can start the instant the tool-router resolves — it doesn't need the search result.

## Decision

Add a streaming, parallel variant for the voice + `action=play` path only.

**New method** `_handle_music_voice_streaming` (`brain/poob.py`):

1. Fan out `_music_handler(...)` as an `asyncio.create_task` — it runs in the background (search + resolve + deferred queue).
2. Concurrently start `_stream_toob_wrap_from_query(user_message, ...)` — a streaming Groq call (`llama-3.1-8b-instant`, `stream=True`) that yields sentences via `_stream_sentences_from_chunks` as tokens arrive.
3. Yield wrap sentences to the session loop → session synthesizes → session plays. The user hears Toob within ~500ms.
4. After the wrap finishes, `await music_task` to ensure the queue resolution either completed or raised.
5. If it raised or the response signals failure (doesn't start with `Playing` / `Queued` / `[SILENT]`), yield a recovery sentence (`"couldn't find it, chief"`).

The existing deferred-playback logic in the session ([[voice-architecture]]) then starts the music after TTS completes, unchanged.

**Non-play actions** (skip/pause/stop/volume) still use the blocking `_handle_music` path. They return `[SILENT]...` in tens of milliseconds; speculative wrap would be pure overhead.

**Hallucination guard** from [[music-tool-hallucination]] is mirrored in the streaming variant — if the query has no token overlap with the user's current message, the call is dropped before anything is spawned.

## Alternatives considered

- **Wrap speculatively but still block the session on music_task.** Defeats the purpose — session would wait for search regardless.
- **Move ytdl inside the wrap LLM via tool use.** Multiple hops, slower, fragile.
- **Pre-resolve top-N YouTube results on every utterance.** Wasteful traffic, flaky, introduces IP/quota risk.
- **Streaming TTS model (Orpheus / Cartesia)** from the research report. Bigger effort, separate decision later. This change slots in cleanly underneath whatever TTS we run.

## Correctness tradeoff

Because the wrap is generated before the search resolves, the wrap references the *request* not the *specific result*. Report estimate: ~2% of calls will experience a wrap-vs-result mismatch (search finds a cover / remix / different-artist version). Acceptable because:

- Toob reacts to "your taste in music," not the track ID — mismatches feel natural.
- The fallback "couldn't find it, chief" is played when the search outright fails.
- Pathological cases are self-correcting: user hears Toob mock the song, user hears music start, user moves on.

## Baseline (to measure)

- Median time from end-of-utterance → `Playing audio` (first TTS frame) on music-play: **TBD** (prod logs).
- Wrap-mismatch rate: **TBD** (subjective, count from prod transcripts).

## Exit criterion

Flips to `active` after the user has run a handful of music-play requests and confirms:

- **Primary**: time-to-first-Toob-word is visibly faster — subjective "snappier" on a clean start.
- **Guard**: no regression on non-play voice actions (skip/pause/stop/volume still instant).
- **Guard**: no new class of error logs (`Voice music route (streaming) failed`, `Speculative wrap failed`, `Music handler (background) failed`) at elevated rates.
- **Guard**: hallucination-drop counter (`music.play hallucinated from context — drop`) stays flat.

If the speculative wrap feels worse (persistent mismatch, awkward silences, crashes) — flip to `superseded`, revert the respond_streaming branch to call `_handle_music` directly for play as well.

## Rollback

Revert the `action == "play"` branch in `respond_streaming` to unconditionally call `_handle_music`. The streaming helpers (`_stream_toob_wrap_from_query`, `_handle_music_voice_streaming`, `_stream_sentences_from_chunks`) can stay — they're inert unless called.

## Session synth-as-yield (2026-04-23 follow-up)

The first version of this decision landed the brain-side concurrency but capped the win: `session._process_single_response` was collecting the full sentence stream before starting synth. That made the brain-side `await music_task` block session's TTS. Both halves are now aligned:

- **Session** (`voice/session.py:_process_single_response`): rewritten to synth + play each sentence as it yields from `respond_streaming`. The previous cross-sentence pipelining (`next_audio_task`) is dropped. For Toob (one sentence max) the pipelining never helped; for casual Poob (1-2 sentences, ~20 words) the simpler loop captures most of the benefit and unlocks speculative streaming.
- **Brain** (`_handle_music_voice_streaming`): no longer awaits `music_task` after the wrap finishes. Attaches a done-callback for background observability (`music.response` / `Music handler (background) failed`), and does a non-blocking `music_task.done()` check at the end — if it finished with a failure we still yield a recovery sentence, otherwise the task runs in the background and the session ends its iteration promptly.

The combined effect: session starts TTS synth within ~300-500ms of end-of-speech instead of after the whole stream (including music_task) resolves.

## Results

<!-- Filled in after real voice use. -->
