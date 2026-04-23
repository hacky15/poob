---
type: incident
status: resolved
date: 2026-04-22
tags: [discord, pycord, slash-commands, startup]
related: [[one-handler-discord]]
---

# `/join` silently unregistered — Pycord sync fired before cogs loaded

## Symptom

User reported `/join` doesn't work. Container log showed the actual attempt was against `!join`:

```
discord.ext.commands.errors.CommandNotFound: Command "join" is not found
```

`!join` was intentionally removed in the one-handler refactor. But `/join` also wasn't reaching Discord's API — the slash commands simply were never registered.

## Root cause

Pycord's `commands.Bot` auto-syncs slash commands in the `on_connect` event. Our `ScraperBot` overrides `on_ready` to load cogs (Pycord has no `setup_hook` equivalent), and `on_ready` fires **after** `on_connect`.

Ordering:

1. `on_connect` → Pycord auto-sync runs → zero slash commands exist yet → nothing registers.
2. `on_ready` → `_load_cogs()` adds `VoiceCog` with `@discord.slash_command` decorators.
3. Commands sit in the local cache, never reach Discord.

## Fix

Explicit `self.sync_commands()` call at the end of `_load_cogs()` in `on_ready`. Syncs **per-guild** — global sync takes up to 1 hour to propagate, unusable for iterative work. Per-guild sync is instant for every guild the bot is currently in.

New commands in guilds the bot joins after startup only register on the next restart. Acceptable tradeoff for the iteration-speed gain. If it becomes a pain, add an `on_guild_join` handler that calls `sync_commands(guild_ids=[guild.id])`.

## Validation

- Post-deploy log shows `Synced slash commands per-guild guilds=3` after `Registered persistent MusicControlsView`.
- `/join` now autocompletes in the Discord client immediately.

## Follow-ups

Captured as a gotcha: [[pycord-auto-sync-commands-fires-before-cogs]].
