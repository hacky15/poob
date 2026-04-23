---
type: decision
status: active
date: 2026-04-21
tags: [music, discord, llm-tool-calling]
related: [[one-handler-discord]] [[music-player-architecture]]
---

# One handler per music action — `handle_music_request(tool_args=...)`

## Context

Music commands arrived from three different input surfaces (voice, text @mention, button click) but had drifted into separate code paths. Divergent behavior (auto-join rules, state refresh, response wrapping) became hard to reason about.

## Decision

**All music actions flow through `MusicCog.handle_music_request(..., tool_args={...})`. No other entry point.**

Three input modalities produce the same `tool_args`:

| Source | How `tool_args` is built |
|---|---|
| Voice ("hey poob skip") | STT → `PoobBrain.respond_streaming` → LLM `music_assistant` tool call → handler |
| Text @mention (`@Poob skip`) | `PoobBrain.respond` → same LLM path → handler |
| Button click (⏭ on now-playing embed) | `MusicControlsView` callback builds `{"action": "skip"}` → handler |

The LLM's sole job is **classifying unstructured speech/text into structured `tool_args`**. Buttons bypass the LLM because their intent is already classified — identical downstream contract, no divergent code paths.

## State sync

`handle_music_request` refreshes `PoobBrain._music_playing_info` from live player state at the top of every invocation. Button clicks therefore keep the brain's LLM-routing context accurate even though they skip the LLM itself. Player state is the single source of truth; brain reads, never caches.

## Command surface

All prefix commands (`!play`, `!skip`, `!queue`, etc.) deleted. Slash commands `/join` and `/leave` remain as the only command surface — they're VC-entry infrastructure (can't @mention the bot in VC if it's not connected yet). All other interactions flow through @mention → LLM → tool_args, or button → tool_args.

## Now-playing UI

`src/poob/discord_bot/music_ui.py`:

- **`build_now_playing_embed(track, queue_size)`** — post-Rythm embed convention: hyperlinked title, top-right thumbnail from `info_dict['thumbnail']` (not hardcoded `maxresdefault` — that 404s on ~15% of videos), duration, requester footer, single brand color.
- **`MusicControlsView(timeout=None)`** — persistent ActionRow with stable `custom_id`s (`poob:music:skip`, `poob:music:pause`, etc.). Re-registered via `bot.add_view(...)` in `on_ready` so buttons survive restarts. Callbacks build `tool_args` and call the handler.

Deliberate non-goals: no live-updating progress bar (Discord's 5-edit/5-second per-channel rate limit makes it counterproductive), no Components V2 containers (overkill for a single-song card).

## Consequences

- Adding a new music source (e.g., a scheduled "rotate in a theme song") means building `tool_args` and calling the handler — no new surface area.
- Control commands return `[SILENT]Status message` from the handler. Voice mode returns empty string (no TTS); text mode strips the prefix and replies with the status text.
