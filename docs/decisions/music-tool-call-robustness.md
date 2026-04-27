---
type: decision
status: proposed
date: 2026-04-27
tags: [brain, music, voice, llm-tool-calling, robustness]
related: [[poobbrain-architecture]] [[speculative-music-wrap]] [[music-tool-hallucination]] [[one-handler-music-contract]] [[groq-gpt-oss-20b-swap]]
---

# Music tool-call robustness pass — token cap, prompt isolation, dedup

## Context

Three failure modes observed in production after the recent perf push:

1. **gpt-oss-20b emitted truncated tool-call JSON.** `failed_generation: '{"name":"music_assistant","arguments":{"action":""}'` — the JSON was cut off mid-arguments. Cause: when `max_tokens_voice` was tightened to 80 to limit response speaking length, the same value was being passed through to tool-call emission. 80 tokens isn't enough for a clean tool-call envelope, especially for `deal_assistant` which embeds the verbatim user message.

2. **Addressee-instruction template leaked into tool args.** Example: user said *"Hey, Poob. Play low by..."* and the LLM emitted `query='low by\n(If you address them by name at all, use ONLY "Ben" — never another user...)'`. The session-level addressee-instruction parenthetical was being concatenated AFTER the user's transcript, and the LLM treated it as part of the user's intent. The same instruction is already in the system prompt, so the user-message version was redundant and load-bearing harm.

3. **Same song queued twice on rapid retries.** Users repeated *"Hey, Poob, play X"* 8-11 seconds later because Toob's reply ducks music to 25% during speech, and the request can read as silent. Both wake events are legitimately addressed; both queue the same track. No deduplication existed.

## Decisions

### A. Tool-call max_tokens is independent of response length

New module-level constant `_TOOL_DETECTION_MAX_TOKENS = 256` in `brain/poob.py`. `_groq_with_tools` clamps the per-provider call to `max(max_tokens, _TOOL_DETECTION_MAX_TOKENS)` so the tool-routing JSON always has enough headroom.

256 was chosen as a power-of-2 cap with cushion: deal_assistant payloads ~120 tokens (verbatim user request), music_assistant ~30-50, JSON envelope ~30. Total worst-case ~200; 256 leaves comfortable margin and is the standard small-tool-call budget.

The casual-streaming path keeps `max_tokens_voice` (=80) untouched — that legitimately governs spoken response length and the brutally-tight cap there was the right call.

**Why not split into two separate config fields:** Considered `tool_detection_max_tokens` as an `AppConfig` field. Rejected as over-engineering — this value is purely tied to LLM tool-call JSON shape, not user preference. A module-level constant is the right scope.

### B. Addressee instruction lives only in the system prompt

`session.py:_process_single_response` no longer appends `(If you address them by name at all, use ONLY "X" — never another user's name from the transcript above.)` to the user message. The same rule is already in `_build_system_prompt` and applies on every voice turn:

> In voice, avoid vocatives (don't start responses with someone's name). If you absolutely must address someone, use ONLY the name marked as the CURRENT SPEAKER in the prompt — never a name from the passive transcript.

The user-message wrap retains the unambiguous identification:

```
[Recent conversation you've been listening to:
<context>
]

=== The user speaking to you RIGHT NOW is {user_name} ===
{user_name} just said to you: {transcript}
```

Trailing line is the user's transcript — the LLM treats it as the request. No instruction text appears after it, so no instruction text leaks into tool_args.

`_split_context` in `brain/poob.py` (see `_CONTEXT_RE`) still parses correctly because the regex is `(?P<message>.+)$` with DOTALL — it captures only what follows the last `said to you:` marker.

### C. Duplicate-play suppression with failure-recovery

Two new helpers on `PoobBrain`:

- `_is_duplicate_play(user_id, query)` — returns True if the same user issued an identical (normalized) play within `_DEDUP_WINDOW_S` (20s). Always records the new attempt regardless of outcome.
- `_clear_play_on_failure(user_id, query)` — drops the dedup record if the music handler returned a failure response (anything not starting with `Playing`/`Queued`/`[SILENT]`) or raised. Ensures a retry-after-failure isn't blocked.

Wired into both `_handle_music` (text channels + voice control commands) and `_handle_music_voice_streaming` (speculative-wrap voice path). The failure-recovery hooks live:

- In `_handle_music`: after `await self._music_handler(...)`, inspect the response and clear on non-success or exception.
- In `_handle_music_voice_streaming`: in the `music_task.add_done_callback` handler, since the brain doesn't await the task (would defeat speculative wrap).

`_DEDUP_WINDOW_S = 20.0` — bounded by the typical Toob speak time (~3-5s) plus retry latency for a user who re-issues; long enough to absorb genuine "I didn't hear an ack" patterns without locking out an intentional repeat.

**Why not at `MusicCog.handle_music_request`:** considered. Rejected because button clicks (`MusicControlsView`) are deliberate user actions and shouldn't be deduplicated against an LLM-routed retry. The brain layer is the right place: it's the LLM-routing surface, where retry-because-no-ack actually happens. Buttons bypass dedup correctly.

## Architectural invariants preserved

- **One-handler music contract** ([[one-handler-music-contract]]) — all three fixes operate at or above the brain layer; `MusicCog.handle_music_request` is unchanged. The contract that "all music actions flow through one handler" is preserved.
- **Speculative wrap** ([[speculative-music-wrap]]) — the dedup check fires *before* `asyncio.create_task(music_handler)`, so duplicate plays don't waste a fan-out task. Failure-recovery happens in the existing done-callback infrastructure, no new awaits introduced.
- **Hallucination guard** ([[music-tool-hallucination]]) — runs before dedup. A hallucinated query is dropped first; only legitimate-looking queries reach the dedup check.

## Exit criterion

After a normal voice session post-deploy:

- `Tool detection failed` events drop to ~0 (gpt-oss-20b has enough tokens to emit clean JSON).
- `groq.tool_call_recovered_from_function_tag` events stay rare (recovery still active for Llama-style failures, just not needed for the truncation case).
- No tool-call `query` arguments contain `"(If you address them"` or other instruction-template fragments.
- `music.play duplicate suppressed` log appears when user double-asks for the same track within 20s, and the song queues only once.
- `music.play hallucinated from context — drop` events stay rare (existing behavior).

## Rollback

Each fix is independent:

- A: revert the `_TOOL_DETECTION_MAX_TOKENS` constant + the `max(max_tokens, _TOOL_DETECTION_MAX_TOKENS)` line. Tool calls truncate again on tight max_tokens.
- B: re-add the addressee parenthetical to `session.py:_process_single_response`. Instruction leaks back into tool args.
- C: remove `_is_duplicate_play` calls in both handler paths. Duplicate retries queue the song twice.

## Results

<!-- Filled in after a normal voice session validates each fix subjectively. -->
