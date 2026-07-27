---
type: decision
status: active
date: 2026-06-15
tags: [logging, observability, voice, music, scanner, deploy, infra]
related: [[persistent-voice-log]] [[homelab-host-memory-observability]] [[production-log-access]]
---

# Per-subsystem component logging: JSONL firehose + component field + curated streams

## Context

Auditing one subsystem's logs in isolation was fragile. Two paths existed and both leaked or dropped (see [[robust-subsystem-logging|the plan]]):

1. **`docker logs poob`** (console/structlog stream) — everything interleaved, ANSI color escapes (`ConsoleRenderer(colors=True)` emitted them even in a non-TTY container), and scanner-patrol spam. Usable only after stripping ANSI and fuzzy keyword-filtering — a keyword filter can silently miss lines.
2. **`<log_dir>/voice.log`** (the persistent tee from [[persistent-voice-log]]) — marketplace-free, but its allow-list `("voice", "brain", "poob.voice")` **excluded `music.*`**. A full VC audit reading only `voice.log` missed every music event (queue/skip/autoplay/ytdl) — a core part of the interactive surface.

Root causes in [src/poob/utils/logging.py](../../src/poob/utils/logging.py): no machine-filterable key on events (callers had to regex the rendered `[logger.name]` bracket), ANSI forced on, two timestamp formats (structlog ISO-UTC vs the stdlib `%(asctime)s` local-time voice.log formatter), and a hardcoded prefix tuple rather than a general router.

## Decision

Extend the existing tee pattern (not a rewrite; $0, no new deps) so any agent can pull exactly one subsystem, losslessly, in one command. One structlog event stream fans out to three sinks:

1. **`component` field on every event.** A processor (`_add_component`) stamps `component` = first dotted segment of the bound logger name. The stdlib voice loggers, which sit outside the structlog vocabulary, are aliased back to `voice` (`poob.voice.*` → `voice`, `discord.voice_client` → `voice`) so a single `component=="voice"` filter still catches the DAVE / WS-4014 events. This is the one key every downstream filter selects on.
2. **`<log_dir>/poob.jsonl` — the machine firehose.** A rotating handler writes **every emitted event** as one JSON object per line (`component`, `timestamp` ISO-UTC, `level`, `logger`, `event`, extras). `log_level` gates the firehose just as it gates the console (INFO in prod, so sub-threshold DEBUG is not captured), but among emitted events nothing is subsystem-filtered or truncated — no ANSI, one timestamp format — so `jq -c 'select(.component=="music")'` is lossless. It also captures the stdlib voice path (voice_compat / discord WS) via an attached handler, so the firehose is a true superset of `voice.log`. exc_info is resolved (while still inside the `except` block) and rendered to a real `exception` traceback string rather than serialized as a useless `<traceback object>` repr. Serialization uses `default=str` + `skipkeys=True`, so an exotic extra (non-serializable value or non-string dict key) degrades that field instead of dropping the line.
3. **Curated human-readable streams — a `{stream: components}` registry.** Replaces the hardcoded prefix tuple. Today one row: `voice` → `(voice, brain, music)` writing `voice.log`. **The music-exclusion gap is closed:** `voice.log` is now the full interactive surface. Extending to a `scanner` stream is a one-row change (registry pattern), but the scanner is already served losslessly by the JSONL firehose, so no second curated file was added.

**ANSI off in non-TTY.** `ConsoleRenderer(colors=_resolve_colors(sys.stdout))`. Colors render only when the actual ConsoleRenderer output stream — stdout, where `PrintLoggerFactory` writes — is an interactive TTY. `docker logs` (a pipe) is clean text; local dev keeps colors. (The plan said `stderr`; the implementation gates on stdout because that is the stream the renderer actually writes to — gating the flag on a different stream than the output would be inconsistent.) **Gotcha:** `ConsoleRenderer`'s default exception formatter is `rich`-colored *independently* of the `colors` flag, so an exception traceback would still emit ANSI into `docker logs` even with `colors=False`. The non-TTY branch therefore also passes `exception_formatter=structlog.dev.plain_traceback`, keeping tracebacks ANSI-free in the container (rich tracebacks stay on for local TTY dev).

Rotation: JSONL 50 MB × 5 (carries everything, incl. scanner); curated streams 25 MB × 4. All on the `poob-data` volume, so they survive Komodo redeploys. Every error in the logging path is swallowed — logging must never break the app.

## Alternatives considered

- **A separate `interactive.log` alongside a frozen `voice.log`.** Rejected — it leaves `voice.log` (the file existing consumers already read) permanently music-blind, so it doesn't actually fix the stated problem for those consumers; they'd have to discover and adopt a new file. Enriching the existing file is additive (same name, same format, more lines) and non-breaking, and directly closes the gap. The cost — the file named `voice.log` now also carries music — is documented here and the name is retained deliberately for consumer compatibility.
- **Drop the curated text files entirely, keep only JSONL + a jq helper.** Rejected for now — a human-readable per-surface tail (`tail -f voice.log`) is still the fastest path during a live voice incident, and keeping `voice.log` honors the "don't break existing consumers" constraint. JSONL is the machine path; `voice.log` is the human path.
- **Per-call `stream=` tag.** Rejected (carried over from [[persistent-voice-log]]) — touches every call site; the logger name already encodes the subsystem.
- **External aggregation (Loki/etc.).** Over-engineered for a single-host homelab on the $0 constraint; a rotating file on the existing volume suffices.

## Consequences

- **`voice.log` now includes music** (voice + brain + music). Additive and non-breaking — existing `tail`/`grep` consumers see the same format with more lines; a VC audit reading only `voice.log` is now complete.
- **`poob.jsonl` is the canonical machine-parseable log.** Filter any subsystem losslessly with `jq` — see [[subsystem-log-queries]] for copy-paste recipes (single subsystem, interactive, time window, errors, grep). Both `voice.log` and the JSONL persist across redeploys.
- **Console/`docker logs` is clean text in the container** (ANSI off in non-TTY) and every line now carries `component=` (machine-greppable even on the raw console stream).
- **The curated router is a registry** — add a `scanner.log` (or any surface) by adding one row to `_CURATED_STREAMS`; no new code path.
- Code asserting on raw structlog output should expect both `logger=` and `component=` fields.
- The deprecated internal API (`setup_voice_log`, `_VOICE_LOGGER_PREFIXES`, `_is_voice_logger`, `_voice_tee_processor`) is removed, not aliased. Setup is now `setup_file_logs` (JSONL + curated), with `_teardown_file_logs` for test isolation.

This extends [[persistent-voice-log]] rather than reversing it — that note's core (a marketplace-free voice log on the data volume capturing both the structlog and stdlib paths) still holds; this widens its scope to music and adds the JSONL firehose + component field.
