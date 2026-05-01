---
type: plan
status: active
date: 2026-04-29
tags: [brain, voice, music, multi-guild, isolation, refactor]
related: [[poobbrain-architecture]] [[voice-architecture]] [[music-player-architecture]] [[one-handler-discord]] [[one-handler-music-contract]]
---

# Plan: PoobBrain multi-guild isolation

## Goal

Poob is now in multiple Discord guilds at once. Each guild's instance must behave as if it were the only one — no shared conversation history, no shared deal-session state, no race-prone scratchpads, no cross-guild music-tool dispatch.

Existing per-guild surfaces (`VoiceCog._sessions`, `MusicCog._players`, per-guild `VoiceSession` and `GuildMusicPlayer`) are already correct. The single concentration of cross-guild leakage is `PoobBrain` itself — it is one shared instance attached to the bot, with `user_id`-keyed and singleton-mutable fields.

## Non-goals

- No per-guild config (different settings per server). Out of scope.
- No cross-guild bridging, broadcasting, or "I see you in N servers" surface.
- No `MusicCog.handle_music_request` signature change — it already accepts `guild_id`.
- No `VoiceSession` factory or wiring changes — already per-guild.
- No `PatrolEngine` / scanner changes — global by design, single bot, single marketplace patrol.

## State inventory — current vs target

### `PoobBrain` fields requiring change

| Field today | Type today | Target |
|---|---|---|
| `_voice_guild_id: int` | singleton | **Removed.** `guild_id` is threaded through method arguments instead. |
| `_music_playing_info: str` | singleton | `_music_playing_info: dict[int, str]` keyed by `guild_id` |
| `_horniness_level: int` | singleton | `_horniness_levels: dict[int, int]` keyed by `guild_id`; helper `_horniness_for(guild_id) -> int` returns rolled value or default `5` |
| `_histories: dict[str, list[_Message]]` keyed by `user_id` | per-user | `_histories: dict[tuple[int, str], list[_Message]]` keyed by `(guild_id, user_id)` |
| `_deal_context: dict[str, str]` keyed by `user_id` | per-user | `_deal_context: dict[tuple[int, str], str]` keyed by `(guild_id, user_id)` |
| `_last_play: dict[str, tuple[str, float]]` keyed by `user_id` | per-user | `_last_play: dict[tuple[int, str], tuple[str, float]]` keyed by `(guild_id, user_id)` |

DM channel sentinel: `guild_id = 0` represents a DM. DMs are naturally isolated because no guild can match `0`.

### `PoobBrain` method signatures requiring change

| Method | Today | Target |
|---|---|---|
| `respond(message, user_id, channel_id="", voice=False)` | no `guild_id` | `respond(message, user_id, channel_id="", voice=False, guild_id=0)` |
| `respond_streaming(message, user_id, channel_id="")` | no `guild_id` | `respond_streaming(message, user_id, channel_id="", guild_id=0)` |
| `_handle_music_voice_streaming(original_message, user_id, max_tok, tool_args)` | reads `self._voice_guild_id` | `_handle_music_voice_streaming(original_message, user_id, max_tok, tool_args, guild_id)` |
| `_handle_music(original_message, user_id, voice, max_tok, tool_args, guild_id=0)` | already takes `guild_id` | unchanged signature; just doesn't fall back to `self._voice_guild_id` anymore |
| `_handle_deal(clean_message, user_id, channel_id, messages, voice, max_tok)` | per-user | adds `guild_id` for `_deal_context` keying |
| `_build_messages(user_id, user_text, voice=False, channel_context="")` | per-user | adds `guild_id` for `_histories` keying and horniness lookup |
| `_save_response(user_id, response)` | per-user | adds `guild_id` for `_histories` keying |
| `clear_history(user_id)` | per-user | overload: `clear_history(user_id, guild_id=None)` — when `guild_id=None`, clear all `(*, user_id)` entries; when set, clear only the specific tuple |
| `roll_horniness()` | global | `roll_horniness(guild_id)` |

### Caller updates

| Caller | Today | Target |
|---|---|---|
| `VoiceSession._process_single_response` calls `self.brain.respond_streaming(prompt, str(user_id))` | no guild | pass `guild_id=self._guild_id` from `voice_client.guild.id` captured on session init |
| `VoiceSession.__init__` | doesn't capture guild | capture `self._guild_id = voice_client.guild.id` |
| `VoiceSession` (anywhere it sets `brain._voice_guild_id`) | mutates singleton | removed; the brain reads `guild_id` from method args |
| `AgentMessageHandler.on_message` calls `brain.respond(message, user_id, channel_id)` | no guild | pass `guild_id=message.guild.id if message.guild else 0` |
| `MusicCog.handle_music_request` (button callback path) | already passes guild_id | unchanged |
| `VoiceCog.roll_horniness()` invocation | global | passes session's guild_id |

### Out-of-scope but worth noting

- `_music_playing_info` is mutated from session-side. The session must update its own guild's slot only. Existing code does `self.brain._music_playing_info = "..."` from `_process_single_response`. Becomes `self.brain._set_music_playing_info(self._guild_id, "...")` via a new helper method that hides the dict.

## Files to change

| File | Change kind |
|---|---|
| `src/poob/brain/poob.py` | Field types, method sigs, internal lookups |
| `src/poob/voice/session.py` | Capture `_guild_id` on init; pass to brain calls; switch `_music_playing_info` mutation to helper |
| `src/poob/discord_bot/agent_handler.py` | Pass `guild_id` to `brain.respond` / `respond_streaming` |
| `src/poob/discord_bot/cogs/voice_cog.py` | Pass guild_id to `roll_horniness` if invoked there |
| `tests/unit/test_brain_*.py` (existing) + new `test_brain_multi_guild.py` | Test matrix below |

No changes to: `MusicCog`, `MusicControlsView`, `GuildMusicPlayer`, `DualPipelineProcessor`, `RealtimeAudioSink`, `voice_compat.py`, scanner, deal agent runner.

## Test matrix

All pure unit tests, no Discord network.

1. **History isolation.** Construct one `PoobBrain`. Call `_save_response` for `user_id="123"` in `guild_id=10` and `guild_id=20`. Assert `_build_messages` for the same user in each guild returns disjoint histories.
2. **Deal-context isolation.** `_deal_context[(10, "123")] = "ctx-A"`, `_deal_context[(20, "123")] = "ctx-B"`. Same user across guilds doesn't cross-contaminate routing.
3. **Last-play dedup is per-guild.** `_is_duplicate_play(guild_id=10, user_id="123", "Bohemian Rhapsody")` → records. Same call for `guild_id=20` returns `False` (not a duplicate). Same call again for `guild_id=10` within window → `True`.
4. **Horniness is per-guild.** `roll_horniness(10)` sets guild 10's level. `_horniness_for(20)` returns default `5`. After `roll_horniness(20)`, the two are independent.
5. **Music-playing info is per-guild.** `_set_music_playing_info(10, "Track A [3:00]")`, `_set_music_playing_info(20, "Track B [4:00]")`. Building a system prompt for guild 10 sees only "Track A".
6. **Concurrent dispatch race.** Two coroutines call `respond_streaming` simultaneously with different `guild_id` and the same `user_id`. The music-routing branch in each must observe its own `guild_id`. Validates that `_voice_guild_id` is gone and there's no global mutable `guild_id` field on the brain.
7. **DM compatibility.** `guild_id=0` for DMs continues to work. A user in DM and in guild 10 with the same `user_id` has two separate histories — DM and guild are isolated.
8. **`clear_history` semantics.** Calling with `(user_id="123")` and `guild_id=None` clears every `(*, "123")` entry. Calling with `(user_id="123", guild_id=10)` clears only `(10, "123")`.

## Migration discipline

- One commit. No partial state.
- Field default factories changed in the dataclass; old singleton fields removed in the same commit.
- Every call site updated; no silent fallthrough using "0" as the implicit guild.
- Compatibility shim: NONE. There's no external consumer of these private fields, and a shim would defeat the isolation invariant. If a test fails because it constructed a `PoobBrain` and accessed an old field directly, the test is the canonical-test bug — fix the test.
- Existing tests that don't touch multi-guild semantics keep passing; they all use `guild_id=0` (DM-like) by default through method-arg defaults.

## Rollback

Single commit revert. No data migration needed because the in-memory dicts are session-bound (recreated on every container start) — there is no on-disk schema to migrate.

## Architecture note follow-up (same commit or next commit)

After implementation: write `docs/architecture/multi-guild-isolation.md` documenting the invariant for future agents:

> **Invariant**: Every mutable state container in `PoobBrain` that depends on user-specific identity must be keyed by `(guild_id, user_id)`. Every guild-specific scratchpad must be keyed by `guild_id`. There are zero singleton mutable fields on `PoobBrain` that affect response routing.

Update [[poobbrain-architecture]] with a `## Multi-guild isolation` section and a link to the new architecture note.

## Acceptance criteria

- All current tests pass.
- New multi-guild test file passes (8 cases above).
- A subjective production validation: Poob is in two test guilds, two different users (or the same user across both) ask for music simultaneously; both queue in the correct guild; neither's wrap text references the other's track; no log contains a music tool routed to the wrong guild.
- The plan note flips to `status: superseded` with `superseded_by: [[multi-guild-isolation]]` once shipped.

## Implementation order

1. Add `(guild_id, user_id)` keys + new fields on `PoobBrain` dataclass.
2. Add `guild_id` parameters to `respond`, `respond_streaming`, `_handle_*`, `_build_messages`, `_save_response`, `clear_history`, `roll_horniness`.
3. Replace `_voice_guild_id` reads with the new arg. Remove the field.
4. Helper `_set_music_playing_info(guild_id, info)` and `_get_music_playing_info(guild_id) -> str`.
5. Update `VoiceSession`: capture `self._guild_id`; pass it to every brain call; replace `self.brain._music_playing_info = ...` with the helper.
6. Update `AgentMessageHandler`: pass `guild_id`.
7. Update `VoiceCog.roll_horniness` call site.
8. Write `tests/unit/test_brain_multi_guild.py`.
9. Run full unit suite. Full pass.
10. Commit.
11. Add architecture note + link from [[poobbrain-architecture]].

No code change ships before steps 1-9 are green locally.
