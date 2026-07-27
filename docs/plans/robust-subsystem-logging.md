---
type: plan
status: resolved
date: 2026-06-15
tags: [logging, observability, voice, scanner, infrastructure]
related: [[persistent-voice-log]] [[homelab-host-memory-observability]] [[per-subsystem-component-logging]] [[subsystem-log-queries]]
---

> [!done] Implemented 2026-06-15 — see [[per-subsystem-component-logging]] (decision) and [[subsystem-log-queries]] (runbook).
> Shipped: `component` field, `poob.jsonl` firehose, ANSI gated on stdout TTY,
> a `{stream: components}` curated registry with `voice.log` = voice+brain+music.
> One deviation from the design below: the curated stream kept the filename
> `voice.log` (enriched with music) rather than a new `interactive.log`, so
> existing consumers aren't broken — rationale in the decision note.

# Robust per-subsystem log filtering

## Problem

An agent auditing one subsystem (VC) cannot cleanly isolate that subsystem's
logs from the others (scanner). Today there are two imperfect paths and both
leak or drop:

1. **`docker logs poob`** (the console/structlog stream) — has *everything*
   interleaved, plus ANSI color escapes (`ConsoleRenderer(colors=True)` emits
   them even in non-TTY Docker) and scanner-patrol spam. Usable only after
   stripping ANSI and keyword-filtering by component — fragile, and a fuzzy
   keyword filter can silently miss lines.
2. **`<log_dir>/voice.log`** (the persistent tee, see [[persistent-voice-log]])
   — marketplace-free, but `_VOICE_LOGGER_PREFIXES = ("voice", "brain",
   "poob.voice")` **excludes `music.*`**. A full VC audit reading only
   `voice.log` would miss every music event (queue/skip/autoplay/ytdl) — i.e.
   it drops a core part of the interactive surface.

Root issues in [src/poob/utils/logging.py](../../src/poob/utils/logging.py):
- No top-level `component` field on events → no clean machine filter; callers
  must regex the `[logger.name]` bracket out of rendered text.
- ANSI colors forced on in the container.
- Two timestamp formats: structlog ISO-UTC (console tee) vs stdlib
  `%(asctime)s` local-time (voice.log formatter).
- The tee allow-list is a hardcoded prefix tuple, not a general router; only
  "voice" exists as a curated stream — no symmetric "scanner" or "all
  interactive (voice+brain+music)".

## Goal

Any agent (or human) can pull exactly one subsystem's logs — voice, brain,
music, scanner, discord — with zero cross-contamination and zero missed lines,
in one command, machine-parseable.

## Proposed design (for the implementing session)

Keep structlog; this is an extension of the existing tee pattern, not a
rewrite. $0 — no new deps, no SaaS.

1. **Add a `component` field to every event.** A structlog processor derives
   it from the first segment of the bound logger name (`voice.session` →
   `component="voice"`, `scanner.patrol` → `component="scanner"`). This is the
   single key everything else filters on.
2. **JSONL sink for machines.** Write `<log_dir>/poob.jsonl` (rotating) with
   ALL events as one JSON object per line including `component`, `timestamp`
   (ISO-UTC), `level`, `event`, and extras. Filtering becomes trivial and
   lossless: `jq -c 'select(.component=="music")' poob.jsonl`. No ANSI, one
   timestamp format, nothing dropped.
3. **ANSI off in non-TTY.** `ConsoleRenderer(colors=sys.stderr.isatty())` so
   `docker logs` is clean text while local dev keeps colors.
4. **Generalize the curated streams.** Replace the hardcoded voice prefix tuple
   with a small {stream_name -> [component, ...]} map driving per-stream
   RotatingFileHandlers, e.g. `interactive.log = voice+brain+music`,
   `scanner.log = scanner`. (Or drop the curated files entirely in favor of the
   JSONL + a filter helper — decide in implementation.) Whatever the choice,
   the interactive stream MUST include `music`.
5. **Query helper / runbook.** A `docs/runbooks/` note (and optionally a tiny
   `scripts/logs.sh`/poob-logs skill update) with copy-paste commands:
   `docker exec poob sh -c "cat /app/data/logs/poob.jsonl" | jq 'select(.component=="voice" or .component=="music")'`
   plus time-window and grep recipes.

## Acceptance

- One command yields ONLY the chosen subsystem(s), no ANSI, no scanner noise,
  no missing component (verified: music events appear in the interactive pull).
- Round-trip JSON parses with `jq` for the whole file.
- Console (`docker logs`) is clean text in the container.
- Decision note written; [[persistent-voice-log]] superseded/updated; the
  `music`-exclusion gap explicitly closed.

## Handoff prompt (paste into a fresh session)

> Poob's logging can't cleanly isolate one subsystem's logs from another's.
> `voice.log` excludes `music.*` (so VC audits miss music) and `docker logs`
> is ANSI-cluttered + scanner-spammed. Read
> [src/poob/utils/logging.py](src/poob/utils/logging.py) and
> docs/decisions/persistent-voice-log.md, then implement robust per-subsystem
> logging per docs/plans/robust-subsystem-logging.md: add a `component` field
> (first segment of the logger name), a rotating JSONL sink with all events,
> turn ANSI off when stderr isn't a TTY, generalize the curated tee so the
> interactive stream includes voice+brain+music, and add a query runbook.
> TDD, then write the decision note and update persistent-voice-log. $0, no new
> deps. Don't break the existing voice.log consumers until the replacement is
> verified.
