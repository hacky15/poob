---
type: decision
status: active
date: 2026-05-12
tags: [music, ui, embed, buttons, pycord, cross-cog]
related: [[music-queue-primitives]] [[music-filter-presets]] [[music-player-architecture]] [[one-handler-music-contract]]
---

# Now-playing embed — three new buttons (previous / replay / leave) + active-effect indicator

## Context

The roadmap [[music-bot-feature-roadmap]] ranked #3 ("now-playing embed with buttons") as the biggest **visible** UX leap — it's what signals "real music bot" vs "voice toy" to a user dropping into the server for the first time. The existing scaffolding already covered the basics: a `MusicControlsView` persistent View with pause / resume / skip / stop / shuffle / loop / queue, registered once in `bot._register_persistent_views()` and re-attached automatically across restarts via stable `custom_id` strings.

What was missing was the user-discoverable surface for the recently-added actions (`previous` and `replay` from [[music-queue-primitives]]) and a clean way to leave the voice channel from the embed itself rather than typing `/leave`. Plus the embed didn't show when an audio effect was active — so users could be listening to nightcored audio with no on-screen confirmation.

## Decision

Three new buttons on `MusicControlsView` plus one embed enhancement plus one cross-cog handler branch. All keep the existing one-handler-music-contract invariant: every button dispatches the same structured `tool_args` the LLM emits.

### New buttons (row 2)

| Custom ID | Emoji | Label | Style | Action |
|---|---|---|---|---|
| `poob:music:previous` | ⏮ | — | secondary | `previous` |
| `poob:music:replay` | 🔄 | — | secondary | `replay` |
| `poob:music:leave` | 🚪 | Leave | **danger** | `leave` |

The button decorators sit in the existing `MusicControlsView` class so the persistent-view registration in `bot._register_persistent_views()` automatically picks them up — no wiring change needed at the bot level. The custom_ids follow the existing `poob:music:*` namespace so re-registration keys are stable across restarts.

### Active-effect indicator

`build_now_playing_embed` gains a keyword-only `active_effect: str = "none"` parameter. When the value is anything other than `"none"`, a third inline field renders next to Duration / Up Next:

```
Effect    Duration   Up Next
nightcore   3:42     2 tracks queued
```

`build_now_playing_message` passes `player.active_effect` automatically, so the AgentMessageHandler's track-change post path picks up the indicator without any caller change. The underscore in preset names is humanized (`slowed_reverb` → `slowed reverb`) so the embed reads naturally.

### Cross-cog `leave` action

The `leave` action lives on the music tool because the embed lives with music — but the actual disconnect path lives in `VoiceCog._force_disconnect` (which knows how to tear down the recording sink, dual-pipeline, voice session state). The handler reaches across via `self.bot.get_cog("Voice")`, the same pattern `AgentMessageHandler` already uses for `speak_if_in_channel`.

Order matters: stop music first (so the audio thread exits cleanly), then disconnect the VC. Missing-VoiceCog branch is defensive — it shouldn't happen given the music+voice config gates in `bot.py`, but a `bot.get_cog` returning `None` is the kind of edge case that crashes a button silently if you don't handle it.

`leave` also goes into the `MUSIC_TOOL` action enum so voice utterances like "Hey Poob, leave the channel" route through the same handler.

## Alternatives considered

- **State-aware button styling** (loop button highlighted when LOOP_ONE is active; shuffle button success-styled when shuffle is on). Tempting, but persistent views CAN'T be updated after the message is posted — the Pycord pattern requires re-sending a new message to change a button's style. The audit confirmed this is the design trade-off the existing implementation already accepted. State feedback comes through the ephemeral `[SILENT]Loop mode: looping current track.` reply each click produces. Not worth breaking the persistent-view invariant for v1.
- **Progress bar in the embed description.** The embed re-renders only on track change, so a static `[████░░░░] 2:30 / 5:45` would show 0:00 every time. Live updates would hit Discord's 5-edit/5-second per-channel rate limit. Skipped per the existing design note at music_ui.py:12.
- **Combine `replay` and `loop_one` into one button.** They feel similar but they're not — `replay` is "restart current track once, then continue normally", while `loop_one` toggles infinite repeat. Keeping them distinct matches what FredBoat / Jockie / Rythm do.
- **Put `leave` in row 0 next to stop.** Considered. Rejected because `leave` is a heavier action (full VC teardown vs music-only stop) and the danger-style red button stands out better when it has space in its own row. Stop in row 0 is also `danger` — having two danger buttons adjacent invites mis-click.

## Consequences

- The persistent View now has 10 buttons across 3 rows. Pycord's hard cap is 25 buttons per View (5 per row × 5 rows); we're well under.
- Cross-cog dependency: `MusicCog.handle_music_request` now reads `self.bot.get_cog("Voice")`. The pattern matches `AgentMessageHandler`. Verified via test that the missing-VoiceCog path doesn't crash.
- Effect indicator only appears in NEW embeds posted after the effect is applied — old embeds still show the prior state. That's the inherent persistent-view limitation; document it but don't try to "fix" it by posting a new embed on every `apply_effect` call (would spam the channel).
- Old embeds posted before this commit still work — the `add_view` registration uses custom_ids that map to the new handlers; clicking a previous/replay/leave button on a brand new embed routes correctly. The OLD embeds (from before this deploy) won't HAVE those buttons because they were rendered with the prior View instance, but their pause/skip/etc. buttons keep working.

## Validation

- 12 new tests in `tests/unit/test_music_ui_extensions.py`:
  - 4 button registration / regression-guard tests (the 3 new + existing-buttons-present).
  - 4 button dispatch tests (previous / replay / leave / silent-prefix-stripping).
  - 3 embed effect-indicator tests (no field on none / field on active / default kwarg).
- 2 new tests in `tests/unit/test_music_handler_actions.py`:
  - `leave` calls player.stop AND voice_cog._force_disconnect in the right order.
  - `leave` doesn't crash when voice_cog is unavailable.
- Schema regression guard in `test_music_handler_actions.py` updated to require `leave` in the action enum.

## Rollback

Remove the three `@discord.ui.button(...)` blocks from `MusicControlsView` (rows 2). Remove the `leave` action branch from the handler and the action from the schema enum. Remove the `active_effect` kwarg from `build_now_playing_embed` (callers default-fall through). The persistent-view registration in `bot.py` doesn't need to change — `add_view` with a `MusicControlsView()` instance is correct regardless of how many buttons that view defines internally.
