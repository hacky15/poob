---
type: decision
status: active
date: 2026-06-01
tags: [voice, deploy, restart, persistence, pycord]
related: [[voice-architecture]] [[voice-4014-reconnect-event-loop-wedge]] [[deploy-flow]] [[auto-join-missed-listening-setup]]
---

# Poob auto-rejoins voice channels after a restart

## Context

A redeploy or crash-restart recreates the bot process, dropping all voice connections. Until now the operator had to `/join` again every time — painful, and acutely so when *deploying a fix itself* kicks Poob out of an active session. Operator ask: "if Poob ever redeploys, he just works as is even if he's already in a VC, so he doesn't have to be kicked and rejoined."

## Decision

Persist Poob's active VC membership and auto-rejoin on the next boot.

- **Store:** `VoiceStateStore` ([voice/voice_state_store.py](../../src/poob/voice/voice_state_store.py)) — a thread-safe JSON map `{guild_id: channel_id}` at `data/voice_state.json` (on the `poob-data` volume, so it **survives container recreation**). All ops best-effort; never raise.
- **Record on join:** in `VoiceCog.setup_session_for_vc` (the single chokepoint used by both `/join` and music auto-join), after the session is created.
- **Forget on leave:** in `VoiceCog._force_disconnect` (the chokepoint for `/leave`, the everyone-left auto-disconnect, and failed joins). So a deliberate leave doesn't auto-rejoin.
- **Restore on boot:** `VoiceCog.restore_sessions()`, called once from `ScraperBot.on_ready` after cogs load. For each persisted pair it connects + runs the same `setup_session_for_vc` (wake/STT/DAVE) `/join` uses. `play_entrance=False` — a restart resume shouldn't announce itself.

## Failure handling

- **Channel gone** (deleted / not a voice channel) → forget it (permanent).
- **Transient connect/setup failure** → keep persisted and retry on the next restart (don't strand the membership on a blip).
- Already-connected guild → skip. Whole restore is wrapped so a failure never breaks boot.

## Alternatives considered

- **Discord auto-resume.** There is none for bot voice — a new process must reconnect explicitly.
- **DB table instead of JSON.** Overkill for a tiny guild→channel map; a JSON file on the existing volume is simpler and equally durable.
- **Reconnect inside the voice WS layer.** Wrong layer — that handles a live 4014 ([[voice-4014-reconnect-event-loop-wedge]]); this is process-restart resume, which needs persisted state + a fresh `on_ready` connect.

## Consequences

- Deploys (including shipping voice fixes) no longer require a manual `/join` — Poob rejoins the channel(s) it was in, with the listening pipeline wired, within a few seconds of boot.
- `data/voice_state.json` is now part of voice state; it's pruned on leave and self-heals from corruption (load returns empty on bad JSON, next record overwrites).
- Pairs nicely with [[voice-4014-reconnect-event-loop-wedge]]: the DAVE-lock fix prevents the wedge, the persistent voice log preserves the evidence, and this makes the recovery restart seamless.
- TDD: `tests/unit/test_voice_state_store.py` (8) — roundtrip, persists-across-instances, per-guild update, forget isolation, missing/corrupt file, write-failure swallowed.
