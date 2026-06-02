---
type: gotcha
status: active
date: 2026-06-02
tags: [cross-workstream, discord, observability, voice-workstream]
related: [[proactive-health-heartbeat]] [[watchlist-sweep-unprotected-cdp-wedge]]
---

# Cross-workstream handoff: make the owner DM honest (discord_bot/ owns this)

**For the Discord/voice workstream** — the scanner workstream added a proactive
health heartbeat ([[proactive-health-heartbeat]]) but deliberately did **not**
edit `src/poob/discord_bot/bot.py` to avoid colliding with in-flight voice work.
Two improvements belong to whoever owns `discord_bot/`:

1. **The startup "Poob is alive 🍑" DM actively misleads.** `ScraperBot._notify_owner_alive` (bot.py:283, fired from `on_ready`) sends an unconditional "alive" ping that conveys zero health — it fired 6s **after** `FB authentication result status=failed` three times during the 2026-06-02 15.5h dark-out, lulling the operator. Make it a real status line: `auth={logged_in|FAILED}`, `last_delivery=<ago>`, `restart_reason=<from PersistentKV health:last_force_exit_reason>`.

2. **Generalize the owner-DM path.** Extract `async def dm_owner(self, text)` from `_notify_owner_alive` so the heartbeat (currently DMing via the bot client from `main.py`) and the startup status both reuse one tested codepath. Then the `main.py` heartbeat loop can call `bot.dm_owner(...)` instead of `bot.get_user(...).send(...)`.

The scanner side is done and live: it writes durable signals to `PersistentKV`
(`health:last_force_exit_reason`, `health:last_delivery_at`) and `main.py` reads
them via `poob.scanner.health_monitor.pending_health_alerts`. This note only
covers the Discord-side polish; the heartbeat already works without it.
