---
type: architecture
status: active
date: 2026-04-29
tags: [brain, voice, music, multi-guild, isolation]
related: [[poobbrain-architecture]] [[voice-architecture]] [[music-player-architecture]] [[one-handler-discord]] [[one-handler-music-contract]]
---

# Multi-guild isolation

## Purpose

Poob runs in multiple Discord guilds at once. Each guild's instance must behave as if it were the only one — no shared conversation history, no shared deal-session state, no race-prone scratchpads, no cross-guild music-tool dispatch. Per-(guild, user) state is the unit; per-guild state is the secondary unit; cross-guild singletons are forbidden for anything that affects response routing or content.

## Invariant

Every mutable state container in `PoobBrain` that depends on user identity must be keyed by `(guild_id, user_id)`. Every guild-specific scratchpad must be keyed by `guild_id`. There are zero singleton mutable fields on `PoobBrain` that affect response routing.

DMs use `guild_id = 0` as the sentinel — naturally isolated from any real guild because `0` cannot collide with a Discord snowflake.

## What is per-(guild, user)

| Field | Purpose |
|---|---|
| `_histories: dict[tuple[int, str], list[_Message]]` | Conversation history. A user in two guilds gets two histories. |
| `_deal_context: dict[tuple[int, str], str]` | Active deal session for multi-turn routing. Independent across guilds. |
| `_last_play: dict[tuple[int, str], tuple[str, float]]` | Duplicate-play suppression record. Same user can replay the same song in different servers. |

## What is per-guild only

| Field | Purpose |
|---|---|
| `_music_playing_info: dict[int, str]` | Currently-playing track string injected into the LLM system prompt for routing context. Each guild's track is independent. |
| `_horniness_levels: dict[int, int]` | Per-guild voice-session vibe (1-10), rolled when that guild's voice session joins. |

## What is shared (deliberately)

- LLM API keys, model names, deal-agent reference, music-handler callback. Stateless or read-only.
- Tool definitions (`DEAL_TOOL`, `MUSIC_TOOL`). Static.
- Logging, regex compiled patterns, helper utilities.

## What is forbidden

- Any singleton mutable field on `PoobBrain` whose value influences a response. The old `_voice_guild_id` and `_horniness_level` are **gone** and must not return (regression-tested in `tests/unit/test_brain_multi_guild.py`).
- Any cross-method temporary that holds guild context — `guild_id` flows through method arguments only.
- Any `MusicCog` / `VoiceCog` / handler write that touches `PoobBrain` private fields directly. Use the helpers `_set_music_playing_info(guild_id, info)` and `_get_music_playing_info(guild_id)`.

## Method contract

Every public/internal entry point that may mutate or read per-guild state takes a `guild_id: int` parameter. Default `0` only for DM compatibility. Voice sessions capture `self._guild_id` on init from `voice_client.guild.id` and pass it on every brain call. Text handlers read `message.guild.id` (or `0` for DMs).

| Method | Receives `guild_id` |
|---|---|
| `respond` | yes (default 0 for DMs) |
| `respond_streaming` | yes |
| `_handle_music` | yes (also dispatches to MusicCog with this id) |
| `_handle_music_voice_streaming` | yes |
| `_handle_deal` | yes |
| `_build_messages` | yes |
| `_rebuild_messages_no_tools` | yes |
| `_save_response` | yes |
| `_is_duplicate_play` | yes |
| `_clear_play_on_failure` | yes |
| `_update_deal_context` | yes |
| `clear_history` | optional — `None` clears all guilds for that user |
| `roll_horniness` | yes |
| `_wrap_in_personality` | yes |
| `_stream_personality_wrap` | yes |

## How callers obtain `guild_id`

| Caller | Source |
|---|---|
| `VoiceSession` | `self._guild_id`, captured at init from `voice_client.guild.id` |
| `AgentMessageHandler.on_message` | `message.guild.id if message.guild else 0` |
| `MusicCog.handle_music_request` (button callbacks) | the `guild_id` arg already on the contract |
| `VoiceCog.join_voice` (rolling horniness) | local `guild_id` in the slash-command handler |

## Key files

- `src/poob/brain/poob.py` — all state and method contracts.
- `src/poob/voice/session.py` — `self._guild_id` capture; passes to `respond_streaming` and the music-info helper.
- `src/poob/discord_bot/agent_handler.py` — passes `message.guild.id` to `respond`.
- `src/poob/discord_bot/cogs/voice_cog.py` — passes `guild_id` to `roll_horniness`.
- `src/poob/discord_bot/cogs/music_cog.py` — uses `_set_music_playing_info(guild_id, …)` instead of writing the brain field directly.

## What's already correct (no change required)

These were already per-guild from prior architecture work:

- `VoiceCog._sessions: dict[int, VoiceSession]` — one session per guild.
- `MusicCog._players: dict[int, GuildMusicPlayer]` — one mixer/queue per guild.
- `VoiceSession` instance fields (`_user_buffers`, `_addressed_queue`, `_inflight_tasks`, etc.) are scoped to the session, which is per-guild.
- `MusicCog.handle_music_request(guild_id=...)` — single funnel, accepts `guild_id`.
- `MusicControlsView` button handlers — bind to a specific player at construction.

## Tests

`tests/unit/test_brain_multi_guild.py` covers 15 cases:

1. `_save_response` / `_histories` isolated per (guild, user).
2. `_build_messages` returns disjoint user-history per guild.
3. DM (`guild_id=0`) isolated from any guild for the same user.
4. `_update_deal_context` isolated per guild.
5. `_is_duplicate_play` independent per guild.
6. `_clear_play_on_failure` only clears the matching guild.
7. `_horniness_for` defaults to 5 per guild independently.
8. `roll_horniness(g)` only affects guild `g`.
9. `_set_music_playing_info` / `_get_music_playing_info` independent per guild.
10. `_build_messages` injects only THIS guild's music info into the system prompt.
11. **Regression guard**: `_voice_guild_id` does not exist on `PoobBrain`.
12. **Regression guard**: `_horniness_level` (singular) does not exist.
13. `clear_history(user_id)` (no guild) clears every `(*, user_id)` entry, leaves other users alone.
14. `clear_history(user_id, guild_id=g)` clears only `(g, user_id)`.
15. Concurrent `_build_messages` calls for different guilds don't cross-contaminate.

## Known limitations / future work

- `_horniness_levels` and `_music_playing_info` dicts grow unbounded as the bot is added to more guilds. Practically negligible (~16 bytes per guild * a few hundred guilds = trivial), but a guild-leave hook could clean entries. Out of scope here.
- DAVE handshake state (see [[dave-handshake-failure-april2026]]) is per-VoiceClient and orthogonal to this isolation — not affected.

## Source documents

- [[poobbrain-multi-guild-isolation]] — implementation plan that drove this commit.
- [[poobbrain-architecture]] — high-level brain shape; updated with a link to this note.
