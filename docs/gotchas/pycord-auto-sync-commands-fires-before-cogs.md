---
type: gotcha
status: active
date: 2026-04-22
tags: [discord, pycord, slash-commands, startup]
related: [[slash-command-sync-on-ready]]
---

# Pycord auto-sync fires before late-loaded cogs exist

## Trigger

Using `commands.Bot` with `auto_sync_commands=True` (the default) AND loading cogs in `on_ready` instead of a `setup_hook` equivalent.

## Why it happens

Pycord auto-syncs slash commands in `on_connect`. `on_connect` fires **before** `on_ready`. Any cog loaded in `on_ready` registers its slash-command decorators into the local bot cache, but the auto-sync has already run against an empty command list — nothing reaches Discord.

This bites every Pycord `commands.Bot` subclass that uses `on_ready` for cog loading, which is essentially all of them (Pycord doesn't provide a `setup_hook`).

## Don't

- Trust `auto_sync_commands=True` to do the right thing when cogs load in `on_ready`.
- Assume leaving auto-sync on is a no-op — it fires at exactly the wrong moment.
- Sync globally during iterative development — global sync takes up to 1 hour to propagate.

## Do

Call `self.sync_commands()` explicitly at the end of `_load_cogs()` in `on_ready`. Per-guild is instant:

```python
await self._load_cogs()
await self.sync_commands()  # per-guild by default
```

Guilds the bot joins after startup need their own sync. `ScraperBot.on_guild_join` in [bot.py](../../src/poob/discord_bot/bot.py) calls `self.sync_commands(guild_ids=[guild.id])` per-guild on every mid-session invite. Without this handler, a freshly-invited guild sees zero slash commands until the next bot restart.

## Reference

Incident: [[slash-command-sync-on-ready]].
