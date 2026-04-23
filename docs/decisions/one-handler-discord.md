---
type: decision
status: active
date: 2026-04-21
tags: [discord, brain, voice, music]
related: [[one-handler-music-contract]] [[poobbrain-architecture]] [[voice-architecture]]
---

# One handler per input modality in Discord

## Context

Logs showed every @mention producing two Poob messages in chat plus a TTS playback even when the user wasn't in VC. Traced to **two parallel `on_message` listeners**: `AgentMessageHandler.on_message` (always fires on @mention/DM) and a now-removed `VoiceCog.on_message` (fired when the guild had "text-to-voice" mode enabled).

Secondary damage: both listeners called `PoobBrain.respond(user_id=X)` concurrently, racing on the per-user history in `_histories`. VoiceCog passed the bare text without channel context; AgentHandler passed the full transcript. Both mutated history for the same user; within a few turns the LLM was hallucinating names from polluted history (Noah's message → Poob addressing "Ben").

## Decision

**Exactly one owner per input modality.**

- **`AgentMessageHandler`** owns text. It is the only `on_message` listener for @mentions / DMs. Generates response via `PoobBrain.respond`, posts the text, then asks sibling cogs for side effects.
- **`VoiceCog`** owns voice output. Exposes `async def speak_if_in_channel(message, text) -> bool`. `AgentMessageHandler` calls it after posting text; VoiceCog speaks iff `message.author.voice.channel == session.voice_client.channel`. No opt-in toggle — the in-VC check is deterministic.
- **`MusicCog`** owns music state and the now-playing embed. Exposes `build_now_playing_message(guild_id)`. AgentHandler calls it after a music-routing response to attach the embed + persistent View.

Deleted: `VoiceCog.on_message`, `VoiceCog._text_to_voice` set, `!voice` toggle, `!say` command.

## Resulting rules

**Text @mention → voice output gated by VC co-membership.** User @mentions Poob in #general while in VC with Poob → text reply + TTS. User @mentions from DMs or a channel while outside Poob's VC → text only, silent in voice. Consequence: Ben typing outside the VC no longer gets his message auto-spoken; Noah typing from outside the VC no longer blasts Poob's reply into the call.

**Music request from text requires requester in Poob's VC.** Music playback creates audio in VC — it only makes sense if the requester can hear it. `MusicCog.handle_music_request` short-circuits with a polite refusal if the caller isn't in the bot's current VC (`play` action only — skip/pause/volume from buttons bypass this check because the button can only be clicked by someone in the channel anyway). When Poob isn't in any VC yet, `_auto_join_requester_vc(guild, user_id)` joins the *requester's* channel, not "most populated" — preserves the invariant.

## Alternatives considered

- **Toggle per-guild text-to-voice** (the prior model). Opt-in setting that produced cross-channel voice bleed and doubled history state. Removed.
- **Locking `_histories`** to fix the race. Treating the symptom, not the cause. Single-owner model removes the race entirely.

## Consequences

- Voice output is predictable and deterministic; the rule can be explained in one sentence.
- Adding a new input modality (e.g. scheduled triggers) means adding one owner that calls the same `PoobBrain.respond` + side-effect pattern.
