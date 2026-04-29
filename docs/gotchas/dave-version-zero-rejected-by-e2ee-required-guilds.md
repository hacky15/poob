---
type: gotcha
status: active
date: 2026-04-29
tags: [voice, dave, pycord, discord-protocol]
related: [[dave-handshake-failure-april2026]] [[dave-timeout-fail-hard-regression]] [[voice-architecture]]
---

# Setting `VOICE_MAX_DAVE_PROTOCOL_VERSION=0` is rejected on E2EE-required guilds

## Trigger

Setting `VOICE_MAX_DAVE_PROTOCOL_VERSION=0` (or `<=0`) in the bot's environment to "disable DAVE" while connecting to a Discord guild that has **E2EE/DAVE required**.

## Why it happens

When IDENTIFY sends `max_dave_protocol_version: 0`, Discord's voice gateway closes the websocket with **close code 4017, reason "E2EE/DAVE protocol required"**. The reconnect loop fires immediately on every retry:

```
Voice handshake complete
WARNING [VoiceCompat] Voice websocket closed: code=4017 reason=E2EE/DAVE protocol required
ERROR  [discord.voice_client] Failed to connect to voice... Retrying...
discord.errors.ConnectionClosed: Shard ID None WebSocket closed with 4017
```

User-visible symptom: bot appears to be "infinitely joining" the voice channel without ever settling. Eventually one connect may succeed (Discord lets a degraded client through after enough retries) but the bot is still functionally deaf and the join command fails with `Not connected to voice channel`.

## Don't

- Use `VOICE_MAX_DAVE_PROTOCOL_VERSION=0` as a "disable DAVE" mechanism. It does not work on guilds that enforce DAVE.
- Recommend it as a workaround for DAVE handshake bugs without checking guild settings first.

## Do

- Leave `VOICE_MAX_DAVE_PROTOCOL_VERSION` unset (defaults to `davey.DAVE_PROTOCOL_VERSION`).
- If you need to investigate DAVE handshake failures, set `VOICE_COMPAT_DEBUG=1` instead — surfaces binary-frame opcodes without breaking the connection.
- If a real "disable DAVE for everyone" workflow is needed, that has to happen at the guild settings level (server admin disables E2EE), not via this env var.

The `_resolve_max_dave_protocol_version` function now silently overrides `<=0` to the davey default and logs a warning when this happens, so a stuck env var on a deployment can't reproduce the reject loop.

## Reference

- Incident chain: [[dave-handshake-failure-april2026]] — Option A (env=0) confirmed dead.
- Discord voice WS close codes: 4014 disconnected, 4015 voice server crashed, **4017 E2EE/DAVE required** (April 2026 addition; not in older Pycord docs).
