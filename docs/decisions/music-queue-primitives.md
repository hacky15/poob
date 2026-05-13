---
type: decision
status: active
date: 2026-05-12
tags: [music, queue, brain-tool, ux]
related: [[music-player-architecture]] [[one-handler-music-contract]] [[music-on-the-fly-filter-respawn]]
---

# Music queue primitives — move / remove / clear / previous / replay

## Context

The existing music-tool surface covered play / skip / pause / resume / stop / shuffle / loop / volume / now_playing / queue. That's enough for "play and let it go" but not enough for "I changed my mind about the order I queued these in" or "play that last song again." The roadmap ranked queue primitives as the #1 ship — table-stakes for any music bot — and called for ``move``, ``remove``, ``clear``, ``previous``, and ``replay`` as a coherent set.

## Decision

Five new actions on the ``music_assistant`` tool, dispatched by ``MusicCog.handle_music_request``. All operate through the existing structured-tool-args contract (no text parsing); all return ``[SILENT]…`` strings so the brain knows to skip TTS narration on success.

### move(from_position, to_position)

Reorder the upcoming queue. **1-based** indices in the tool args to match the ``format_queue()`` display the user sees; the handler subtracts 1 before calling ``MusicQueue.move(from_idx, to_idx)``. Out-of-range indices return ``[SILENT]Position out of range (queue has N)``. ``move(i, i)`` returns the track at ``i`` (no-op, but a deliberate return so the LLM can confirm).

If the queue is shuffled, ``move`` also re-syncs the saved original-order so a later ``unshuffle`` reflects the manual move. Implementation: ``MusicQueue.move`` removes-and-re-inserts in ``_original_order`` alongside the live ``_queue``.

### remove(position)

Delete a track from the upcoming queue. 1-based, same conversion. Operates on the upcoming queue only; current playback is untouched. Returns ``[SILENT]Removed {title} from the queue.`` Implementation reuses the existing ``MusicQueue.remove(index)`` (which already kept ``_original_order`` consistent).

### clear()

Empty the upcoming queue. Current playback continues. Returns the cleared count: ``[SILENT]Cleared N tracks from the queue.`` Reuses the existing ``MusicQueue.clear()``.

### previous()

Walk back to the most-recently-finished track. Pops the last entry from history, pushes the currently-playing track to the front of the upcoming queue (so it plays after the previous one finishes), and respawns the FFmpeg source at position 0 with the active effect chain. Returns ``[SILENT]Back to {title}.`` Returns ``[SILENT]Nothing in the history to go back to.`` when history is empty.

The split is deliberate: ``MusicQueue.previous()`` is a pure pop from history (no side effects on ``current`` or ``upcoming``), and ``GuildMusicPlayer.previous()`` owns the orchestration (swap, respawn). Without this discipline, ``MusicQueue.get_next()`` would double-advance through history when the player loop wakes up after the ``vc.stop()``, producing wrong history-walker semantics.

### replay()

Restart the current track from the beginning. Implemented via the respawn mechanism: keeps ``queue.current`` set, asks ``_player_loop`` to rebuild the audio source at position 0 with the same active effect chain. Returns ``[SILENT]Restarting {title} from the top.``

## Implementation map

| Layer | Where | What |
|---|---|---|
| Queue model | [music/queue.py](../../src/poob/music/queue.py) | ``move(from_idx, to_idx) -> Track | None`` and ``previous() -> Track | None`` |
| Player orchestration | [music/player.py](../../src/poob/music/player.py) | ``replay() -> Track | None`` and ``previous() -> Track | None`` (uses ``_respawn_request``) |
| Tool schema | [brain/poob.py](../../src/poob/brain/poob.py) | Extend ``MUSIC_TOOL`` action enum + new ``from_position`` / ``to_position`` / ``position`` params |
| Dispatch | [discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) | New branches in ``handle_music_request`` |

## Alternatives considered

- **Have ``MusicQueue.previous()`` mutate ``current`` and ``upcoming`` itself.** Tempting (single call from the player), but conflates two concerns. ``previous()`` is the "history pop" semantic; the player layer owns "stop and restart with the new current." Keeping them separate makes the queue unit-testable without a player + voice client.
- **Put ``replay`` under a generic ``seek`` action with target=0.** Considered, but ``seek`` is a separate roadmap item (#7) and conflating them muddies the dispatch surface. ``replay`` is the cheap, universally-understood action; ``seek`` will land later.
- **0-based indices in the tool args.** Considered. Rejected because every other music bot (and the existing ``format_queue`` output) is 1-based, and the LLM would need to remember to subtract 1 on every call. Keep the surface 1-based; do the arithmetic at the boundary.

## Consequences

- Queue manipulation is now a one-tool surface (the brain emits ``music_assistant`` with the appropriate action). No new top-level tools, no schema churn.
- Tool-hallucination guard ([[tool-hallucination-from-passive-context]]) — only ``play`` and ``queue_many`` actions have token-overlap query checks. ``previous`` / ``replay`` / ``move`` / ``remove`` / ``clear`` are intent-only verbs, not query-bearing, so they're not subject to that class of hallucination. If we observe the LLM mis-firing ``previous`` from passive context, add a "current turn must contain back / previous / earlier" guard at that point.
- Position-tracker + respawn mechanism (now shared by ``replay`` / ``previous`` / ``set_effect``) is documented in [[music-on-the-fly-filter-respawn]].

## Validation

- 11 new tests in ``tests/unit/test_music_queue_primitives.py`` covering ``move`` (reorder, out-of-range, same-position, shuffled-state) and ``previous`` (returns history pop, walks twice, empty history, no-mutation).
- 14 new tests in ``tests/unit/test_music_player_primitives.py`` covering ``replay`` / ``previous`` / ``set_effect`` state transitions + position-tracker math.
- 20 new tests in ``tests/unit/test_music_handler_actions.py`` covering handler dispatch + ``[SILENT]`` reply shape for all new actions.
- Full unit suite: 0 regressions on the existing 906+ baseline.

## Rollback

Remove the new branches from ``handle_music_request`` and revert the schema. ``MusicQueue.move`` / ``MusicQueue.previous`` and ``GuildMusicPlayer.replay`` / ``previous`` can stay — they're inert without a tool to invoke them.
