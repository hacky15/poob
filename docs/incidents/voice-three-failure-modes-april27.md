---
type: incident
status: resolved
date: 2026-04-27
tags: [voice, brain, music, llm, slash-commands]
related: [[voice-architecture]] [[poobbrain-architecture]] [[music-tool-call-robustness]] [[one-handler-discord]]
---

# Three voice failures in one session — `/join` crash, empty-query play, leaked tool-name

## Symptom

Production logs from a single voice session showed three distinct failures:

1. **`/join` slash command crashed** with `AttributeError: 'VoiceSession' object has no attribute '_current_task'` at `session.py:1114`. User couldn't join Poob to a voice channel.
2. **Empty-query play call**: User said "Hey, Poob. Play" — STT cut off mid-sentence. The LLM emitted `{action: "play", query: ""}`. The music handler returned `"What do you want me to play?"`. The success-marker check rejected the response, the speculative-wrap streamed Toob's first sentence, then yielded `"couldn't find it, chief"` as a recovery line — producing the awkward concatenated reply *"...listening to me. couldn't find it, chief."*
3. **Leaked tool-name in casual response**: User said "Bitch. Hey, Poob." (wake without a request). Tool detection found nothing. Casual fall-through generated `"play something by red hot chili peppers, music_assistant."` — emitting the literal tool-name word as plain text.

## Root causes

**(1) Dead `_current_task` attribute.** `VoiceSession.cleanup()` referenced `self._current_task` but no constructor or method ever assigned it. Leftover code from an earlier refactor that introduced `_response_lock` + per-utterance task spawning. Any `/join` that triggered a `_force_disconnect → session.cleanup()` path crashed.

**(2) No empty-query guard before fan-out.** The hallucination guard in `_handle_music` and `_handle_music_voice_streaming` only ran when `query` was non-empty. An empty / one-character query passed through to the music handler, which had no song to look up and returned a clarifying string. The brain's success-marker check then dispatched the failure-recovery line on top of the wrap, producing concatenated nonsense.

**(3) Casual model echoed tool-name as text.** `_build_system_prompt` always included the music_assistant / deal_assistant tool descriptions and examples. When the casual fall-through path used `llama-3.1-8b-instant` without any tools defined, the model still saw the tool descriptions in its system prompt and emitted `music_assistant` literally. The 8b model has no way to actually *call* the tool — it only knows the word from its prompt and treats it as part of the vocabulary.

## Fixes

**(1) Replace `_current_task` with a real in-flight-task set.**

`VoiceSession.__init__` now declares `self._inflight_tasks: set[asyncio.Task] = set()`. The dispatch site in `_on_dual_addressed` registers each task it spawns and uses `task.add_done_callback(self._inflight_tasks.discard)` for self-cleanup. `cleanup()` cancels everything still running:

```python
for task in list(self._inflight_tasks):
    if not task.done():
        task.cancel()
self._inflight_tasks.clear()
```

`/join → _force_disconnect → cleanup()` now exits cleanly even if a session is mid-response.

**(2) Empty-query guard before fan-out.**

In both `_handle_music` and `_handle_music_voice_streaming`, when `tool_args.action == "play"` and `len(query.strip()) < 2`, log `music.play empty query — prompting user`, return a single-sentence `"Play what?"` (text path) or `"play what?"` (voice path), and do NOT fan out to the music handler. No ytdl waste, no awkward recovery-line concatenation.

**(3) Tool-aware vs tool-free system prompt.**

`_build_system_prompt` gained a `with_tools: bool = True` parameter. Tool-routing keeps the existing prompt with tool descriptions. The casual fall-through (`respond_streaming` Step 3) now rebuilds the messages with `with_tools=False` via `_rebuild_messages_no_tools(messages, voice=True)`. The tool-free prompt also adds an explicit rule: *"Mention any internal tool, function, or routing names — these are implementation details and have no place in spoken responses."*

Defense-in-depth: a new `_scrub_tool_leakage` helper strips both `<function=...>` markup AND standalone `music_assistant` / `deal_assistant` tokens from each streamed sentence. If a future model still leaks despite the cleaner prompt, the post-filter catches it.

## Architectural integrity

- **One-handler music contract** ([[one-handler-music-contract]]) — empty-query rejection happens at the brain layer before fan-out, so `MusicCog.handle_music_request` never sees malformed args.
- **Speculative wrap** ([[speculative-music-wrap]]) — empty-query guard fires before the fan-out task is created, so we don't waste an `asyncio.create_task`.
- **Hallucination guard** ([[music-tool-hallucination]]) — runs after the empty-query guard. Empty queries can't pass the lexical-overlap check anyway; the new guard is just earlier and gives a better UX line.
- **Voice session cleanup** — now actually cancels in-flight work instead of crashing on a phantom attribute. Aligns with [[one-handler-discord]] which made `session.cleanup()` callable from `_force_disconnect`.

## Validation

- 43 voice + music + brain unit tests pass.
- AST parse clean on `brain/poob.py` and `voice/session.py`.
- Watch for in production:
  - No `AttributeError: 'VoiceSession' object has no attribute '_current_task'` traces.
  - `music.play empty query — prompting user` logs when STT clips off "Hey Poob play" mid-sentence.
  - No tool-name literals in `Response complete response=...` lines on the casual fall-through path.

## Follow-ups

None of these are bandaids — each fix lives at the root cause layer. If the casual model still leaks tool names occasionally despite the tool-free prompt, the `_scrub_tool_leakage` post-filter handles it without code change.
