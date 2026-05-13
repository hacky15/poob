---
type: decision
status: active
date: 2026-05-12
tags: [music, queue, brain-tool, llm-tool-calling]
related: [[music-queue-primitives]] [[poobbrain-architecture]] [[tool-hallucination-from-passive-context]]
---

# Multi-song queue from one utterance — `queue_many(tracks: list[str])`

## Context

"Queue up Bohemian Rhapsody and Don't Stop Believin' and Africa by Toto" — one utterance, three songs. The pre-existing ``play`` action takes a single ``query``; multi-song requests had to be split into either:

- Multiple sequential ``play`` calls from the LLM (slow, ordering-fragile).
- Server-side regex over the raw user message for "and" / commas / "then" (brittle: "Salt and Pepper" gets split on the wrong "and"; songs with commas in their titles get truncated).

The roadmap's research found that LLM-augmented bots (Aiode and similar) don't actually do this well — they accept one query at a time. The recommended pattern is **(a) LLM emits an array argument in a single tool call**: ``queue_tracks(tracks=["…", "…"])``. That's the move.

## Decision

New ``music_assistant`` action ``queue_many`` with a ``tracks: list[str]`` parameter. Each entry is a separate search query; the handler resolves them serially through ``AsyncYTDL.search``, queues the resolved ones, and reports per-track status so the brain can narrate partial success.

### Tool schema

```json
{
  "action": { "enum": [..., "queue_many", ...] },
  "tracks": {
    "type": "array",
    "items": { "type": "string" },
    "description": "Multiple song titles for 'queue_many'. Each entry is a separate search query — one song per entry, no commas or 'and' chaining within a string."
  }
}
```

The schema description explicitly forbids comma-chaining within a single entry. The LLM's job is to extract the structured list; the bot doesn't try to split it server-side.

### Handler behavior

1. Validate ``tracks_arg`` is a non-empty list. Empty → ``[SILENT]queue_many needs a non-empty 'tracks' list.``
2. For each entry:
   - Strip whitespace.
   - If ``len(title) < 2``, count as ``not_found`` and continue. (STT noise like ``"a"`` or empty strings is a real input class — drop silently rather than search YouTube for one letter.)
   - ``track = await self._ytdl.search(title, ...)``. If ``None``, count as ``not_found``.
   - Else ``await player.play(track, deferred=voice)`` and append to ``resolved``.
3. Build the response from the resolved + not_found counts:
   - All misses → ``Couldn't find any of: A, B, C.`` (non-SILENT, so the brain narrates the failure)
   - Some resolved → ``Queued 3 tracks. Couldn't find: X.`` (also non-SILENT, the brain wraps in personality)
   - When the player was idle before the call, the head becomes ``Playing {first}, queued N more.``

The non-SILENT response on partial / total failure is intentional — those are user-meaningful states that deserve narration. Successful queues are also non-SILENT because the count carries information ("queued 5 tracks") the user wants to hear.

## Why this is a Poob-leapfrog

The roadmap noted: "Aiode and the Lavalink-stack bots don't actually do this well — they accept one query at a time. Poob's already-multi-modal brain is positioned to leapfrog them." Pattern (a) means a single tool call dispatches the whole batch, the LLM only does one round-trip, and the user experience is "Poob heard the request, queued the songs, narrated which ones it found." Compared to multi-turn back-and-forth that competitor bots produce, this is a noticeably better feel.

## Alternatives considered

- **Parallel tool calls** (LLM emits ``play("A")`` + ``play("B")`` + ``play("C")`` in one assistant turn). Works on Groq's ``parallel_tool_calls`` but: (a) ordering isn't guaranteed across parallel calls; (b) Poob's existing brain only routes one tool per turn. Single-call array is simpler and ordering-deterministic.
- **Server-side parse of "and" / commas**. Brittle. See "Salt and Pepper" / commas-in-titles in the roadmap. Don't.
- **Two-stage extract + per-track resolve**. Adds an LLM round-trip for no benefit. The brain's tool args ARE the extraction.

## Hallucination guard

The existing token-overlap check on ``play`` actions ([[tool-hallucination-from-passive-context]]) protects against LLMs pulling a track title from 20-minute-old chatter. ``queue_many`` skips that check because the tool args are an *explicit list* — a partial overlap with one entry doesn't disqualify others. We accept slightly higher false-positive risk on queue_many in exchange for the multi-track UX. Mitigations:

- The 2-character length floor drops obvious garbage entries.
- Per-track ``search`` returns ``None`` for unresolvable text → counted as ``not_found``, no playback side effect.
- The non-SILENT response forces the brain to surface ``Couldn't find: X`` for misses — visible to the user, no silent enqueue of wrong tracks.

If we observe queue_many hallucinations in prod, add the per-track token-overlap check at that point.

## Validation

- 5 new tests in ``tests/unit/test_music_handler_actions.py`` covering: all-resolve, partial-resolve (with miss-name in reply), all-miss, empty-list, length-floor drop.
- Schema regression guard in the same file confirms ``tracks`` is in ``MUSIC_TOOL`` and is an ``array of string``.

## Rollback

Remove the ``queue_many`` action from the schema and the handler. ``MusicQueue.add_many`` and ``GuildMusicPlayer.play_many`` already existed and stay as-is.
