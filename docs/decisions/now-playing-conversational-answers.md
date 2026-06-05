---
type: decision
status: active
date: 2026-06-04
tags: [brain, music, voice, prompt, now-playing, ux]
related: [[now-playing-card-download-race]] [[music-routing-prompt-thoughtfulness]]
---

# Poob answers current-song questions conversationally ("who sings this", "how long")

## Decision

When music is playing and the user *asks about* the current song — its name,
who sings/performs it, the artist, how long it is, how much is left — Poob
answers **conversationally from context**, with no tool call and no search.
Only **control** intents (skip / pause / resume / stop / volume / shuffle /
loop) route to `music_assistant`.

## Why

Operator request: *"I should be able to ask Poob 'what is this song name' /
'what is the length of this song' / 'who sings this song' at any point of it
playing. Poob should just have that information. Not overloaded."*

The track was **already in Poob's prompt** — the voice and music layers call
`_set_music_playing_info(guild_id, "{title} [{duration}]")`, and `_build_messages`
injects it as `[MUSIC IS CURRENTLY PLAYING: …]`. Freshness is not a problem:
the voice path refreshes it **live, per-utterance, right before each response**
([session.py](../../src/poob/voice/session.py)), and the music cog refreshes on
every action ([music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py)) — so
it's correct even across auto-advance.

The blocker was the **prompt instruction itself**: it said *"when the user says
anything about … what's playing — you MUST call music_assistant … Do NOT respond
with text"*, forcing the silent `now_playing` tool (`[SILENT]Now playing: …`)
instead of letting Poob just answer. There was nothing to build data-wise — only
to stop forbidding the answer.

## How

Extracted the inline block into `PoobBrain._music_context_block(music_info)`
(small, pure, unit-testable) and rewrote it to split:

- **CONTROL** → must call `music_assistant` with the action enum, no text.
- **INFO** → *"you ALREADY have the answer on this line, so just SAY it in your
  own voice. Do NOT call a tool and do NOT search for an info question."*

The Track title is `"Artist - Song"` (yt-dlp convention; the `Track` model has
no separate artist field), so "who sings this" is the part before the dash —
the prompt states the format so the LLM can split it. Duration is already on the
line (`[4:03]`). No new tool, no new data path, no extra latency — "not
overloaded."

## Validation

- `tests/unit/test_now_playing_answers.py` (5): info questions → conversational
  (no tool / no search), control still routes to the tool, the live track string
  is surfaced verbatim, `_build_messages` injects the guidance when playing and
  omits the block when nothing plays.
- `tests/unit/test_brain_multi_guild.py` green (per-guild music-info isolation
  unaffected by the refactor).
