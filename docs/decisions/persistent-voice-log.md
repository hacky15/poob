---
type: decision
status: active
date: 2026-06-01
tags: [voice, logging, observability, deploy, infra]
related: [[voice-4014-reconnect-event-loop-wedge]] [[voice-architecture]] [[production-log-access]]
---

# Dedicated persistent voice/chat log on the data volume

## Context

Two recurring debugging pains, both surfaced hard during the 2026-06-01 voice incident:

1. **Marketplace noise drowns voice/chat.** Voice events (`voice.*`, `brain`) and scanner events (`graphql`, `patrol`, `vlm`, `scanner`) share one interleaved stdout stream. Every voice investigation meant a fragile `grep -ivE 'graphql|patrol|vlm|...'` dance that can accidentally drop real voice lines.
2. **Redeploys wipe the logs.** `docker logs` only holds the *current* container's output; a Komodo redeploy recreates the container and the prior logs are gone. The 8-minute event-loop wedge ([[voice-4014-reconnect-event-loop-wedge]]) was lost twice this way — the evidence of the worst failure was unrecoverable.

The bot also has a split logging architecture: structlog (via `PrintLoggerFactory` → stdout) for `voice.*`/`brain`, but **stdlib** logging for `voice_compat` (`poob.voice.voice_compat`) and `discord.voice_client` — and the most critical connection events (the WS 4014 close, DAVE handshake/reconnect) are on the stdlib path.

## Decision

Write a dedicated **`<log_dir>/voice.log`** (default `data/logs/voice.log` → on the `poob-data` volume, so it survives redeploys), capturing the voice/chat surface only:

- **structlog voice/brain events** via a tee processor (`_voice_tee_processor`) inserted before the console renderer. It routes by logger name (`voice*`, `brain*`, `poob.voice*`) and writes through the file handler, leaving console output untouched.
- **stdlib voice events** (voice_compat DAVE, discord voice WS incl. 4014) by attaching the same `RotatingFileHandler` to the `poob.voice` and `discord.voice_client` stdlib loggers; child loggers propagate up to it.

`PrintLoggerFactory` discards the logger name, so `get_logger(name)` now binds `logger=<name>` into the event context — which both feeds the tee's name-routing and makes the console/docker stream show which subsystem emitted each line (a free readability win).

Rotation: 25 MB × 4 backups (~100 MB cap on the volume). All errors in the logging path are swallowed — logging must never break the app.

## Alternatives considered

- **Per-call `stream=voice` tag + filter.** Rejected — touches every voice log call site; invasive and easy to miss.
- **Split scanner.log too (the 2-file option).** Deferred — the immediate need is *seeing voice clearly + surviving redeploys*; the scanner stream is already adequately served by `docker logs` + the `scan_logs` DB table. Easy to add later by extending the same mechanism.
- **External log aggregation (Loki/etc.).** Over-engineered for a single-host homelab + $0 constraint; a rotating file on the existing volume is sufficient.

## Consequences

- A clean, marketplace-free voice/chat log that **persists across redeploys** — the wedge-class failures are now recoverable. Read it with `ssh ben@homelab "docker exec poob tail -200 /app/data/logs/voice.log"` (or `cat` the rotated files), free of scanner spam.
- The console/docker stream is unchanged (still has everything, now with `logger=` tags) — `poob-logs` workflows keep working.
- `get_logger` now returns a name-bound logger; any code asserting on raw structlog output should expect a `logger=` field.
- Captures both logging paths in one file, so a future voice incident has the conversation (structlog) AND the connection/DAVE events (stdlib) side by side.
